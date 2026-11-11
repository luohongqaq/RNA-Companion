"""上下文压缩阶段入口，编排固定来源、摘要生成、校验和发布。"""
from __future__ import annotations
from module.shared.context.types.messages import PromptMessage
from module.context_compaction.core.domain.ports import ContextSummaryModelPort
from logger import stage
from datetime import datetime, timezone
from module.character.core.domain.models import CharacterCard
from module.shared.common.errors.base import InvalidStateError, NotFoundError
from module.context_compaction.core.domain.policy import ContextCompactionPolicy, ContextModelConfig
from module.context_compaction.core.domain.lifecycle_policy import SessionLifecycleConfig
from module.context_compaction.core.domain.ports import ContextMaintenanceRepository
from module.context_compaction.core.processing.prompts.summary_generation_prompt import GENERATION_PROMPT, PROMPT_VERSION as GENERATION_VERSION
from module.context_compaction.core.domain.errors import ContextGenerationRejected, ContextSourceChangedError
from module.context_compaction.core.domain.job import ContextJob, ContextJobStatus
from module.context_compaction.core.domain.source import SummaryModelInput
from module.context_compaction.core.domain.plan import CompactionAssessment, CompactionPlan, TurnTokenInfo
from module.context_compaction.core.domain.lifecycle import LifecycleDecision
from module.shared.context.types.references import InheritedContextRef
from module.shared.context.types.turns import TurnRange
from module.context_compaction.core.domain.references import CompactionMode
from module.context_compaction.core.domain.candidate import SummaryCandidate, SummaryValidation
from module.context_compaction.core.domain.snapshots import SummarySourceManifest
from module.shared.context.types.summary import StructuredSummary, SummaryVersions
from module.chat.reply.core.domain.config.context_budget import ContextPromptBudget
from module.chat.reply.core.domain.ports import ReplyPromptResourcePort, TokenCounter


class ContextCompactionService:
    """统一的交接规划和检查；不导入具体SDK，仓储事务由调用方控制。"""

    def __init__(
        self,
        *,
        prompt_budget: ContextPromptBudget,
        compaction_policy: ContextCompactionPolicy,
        lifecycle_policy: SessionLifecycleConfig | None = None,
        repository: ContextMaintenanceRepository | None = None,
        token_counter: TokenCounter | None = None,
        model: ContextSummaryModelPort | None = None,
        model_config: ContextModelConfig | None = None,
        summary_versions: SummaryVersions | None = None,
        prompt_resources: ReplyPromptResourcePort | None = None,
    ) -> None:
        self._prompt_budget = prompt_budget
        self._compaction_policy = compaction_policy
        self._lifecycle_policy = SessionLifecycleConfig() if lifecycle_policy is None else lifecycle_policy
        self._repository = repository
        self._token_counter = token_counter
        self._model = model
        self._model_config = ContextModelConfig() if model_config is None else model_config
        self._summary_versions = summary_versions
        self._prompt_resources = prompt_resources
        # 对象自身的合法性在配置类校验；这里只检查不同配置之间的对应关系。
        if prompt_budget.history_budget != self._lifecycle_policy.session_target_tokens:
            raise InvalidStateError("历史预算必须与session窗口额度一致")
        if compaction_policy.summary_token_limit >= self._lifecycle_policy.session_target_tokens:
            raise InvalidStateError("交接摘要上限必须小于窗口额度")

    async def process_job(self, job_id: str) -> None:
        """一次推进一个持久化阶段；执行态和终态不重复执行。"""
        repository = self._repository
        if repository is None:
            raise InvalidStateError("处理上下文任务前必须配置仓储端口")
        job = await repository.get_job(job_id)
        if job is None:
            raise NotFoundError("上下文任务不存在")
        if job.available_at > datetime.now(timezone.utc):
            return
        handlers = {
            ContextJobStatus.NEED_GENERATE: self._generate,
            ContextJobStatus.NEED_VALIDATE: self._validate,
            ContextJobStatus.NEED_PUBLISH: self._publish,
        }
        handler = handlers.get(job.status)
        if handler is not None:
            await handler(job)

    @stage("context.generate")
    async def _generate(self, job: ContextJob) -> None:
        """登记执行代次→准备材料→事务外生成→原子保存生成恢复点。"""
        if self._model is None:
            raise NotImplementedError("摘要生成模型调用尚未接入")
        if self._token_counter is None:
            raise InvalidStateError("生成摘要前必须配置token计数器")
        if self._prompt_resources is None:
            raise NotImplementedError("摘要生成的角色资源读取尚未接入")
        versions = self._summary_versions
        if versions is None:
            versions = SummaryVersions(2, prompt_version=GENERATION_VERSION,
                model_name=self._model_config.model_name, tokenizer_version=self._token_counter.tokenizer_version)
        if versions.tokenizer_version != self._token_counter.tokenizer_version:
            raise InvalidStateError("摘要版本信息与token计数器不一致")
        repository = self._repository
        started = await repository.begin_generate(job.job_id)
        if started is None:
            return
        inherited = await repository.read_inherited_context(started.session_id)
        turn_tokens = await repository.read_turn_token_infos(started.session_id, started.frozen_through_turn_seq)
        plan = self.plan_compaction(session_id=started.session_id,
            frozen_through_turn_seq=started.frozen_through_turn_seq,
            turn_tokens=turn_tokens, inherited_context=inherited)
        if plan is None:
            raise InvalidStateError("上下文生成任务没有可压缩的完整轮")
        source = await repository.read_model_input(plan)
        try:
            summary = await self.generate_summary(source, feedback=job.last_error)
        except ContextSourceChangedError:
            raise
        except InvalidStateError as error:
            # 输出不合格受压缩重试额度限制，网络异常由workflow退避。
            raise ContextGenerationRejected(str(error),
                retryable=started.attempt_count <= plan.compaction_policy.max_compression_retries) from error
        candidate = self.build_candidate_summary(summary=summary, manifest=source.source_manifest,
            attempt_version=started.attempt_version, candidate_version=1,
            versions=versions, token_counter=self._token_counter)
        await repository.save_candidate(started.job_id, started.attempt_version, candidate, source=source)

    @stage("context.validate")
    async def _validate(self, job: ContextJob) -> None:
        """登记校验代次；复用已保存候选，保存程序校验恢复点。"""
        if self._token_counter is None:
            raise InvalidStateError("检查摘要前必须配置token计数器")
        repository = self._repository
        started = await repository.begin_validate(job.job_id)
        if started is None:
            return
        source = await self._load_model_input(job=started, repository=repository)
        validation = self.validate_candidate(source, started.candidate, self._token_counter)
        await repository.save_validation(started.job_id, started.attempt_version, validation)

    @stage("context.publish")
    async def _publish(self, job: ContextJob) -> None:
        """一个事务内发布READY快照；交接成功才DONE。"""
        await self._repository.publish_ready_snapshot(job.job_id, job.attempt_version)

    async def _request_model(self, *, prompt: str, payload: dict, model: str) -> dict:
        if self._model is None:
            raise NotImplementedError("上下文模型客户端尚未接入")
        return await self._model.complete(prompt=prompt, payload=payload, model=model, config=self._model_config)

    async def generate_summary(self, source: SummaryModelInput, *, feedback: str | None = None,
                               character_card: CharacterCard | None = None) -> StructuredSummary:
        """加载session所属角色卡，复用记忆召回视图，以角色视角生成交接摘要。"""
        if not source.character_id:
            raise InvalidStateError("摘要来源缺少session所属角色")
        if character_card is None:
            if self._prompt_resources is None:
                raise NotImplementedError("摘要生成的角色资源读取尚未接入")
            resources = await self._prompt_resources.load(source.character_id)
            character_card = resources.character_card
        if character_card.character_id != source.character_id:
            raise InvalidStateError("角色卡与摘要session所属角色不一致")
        payload = source.to_model_payload(character_context=character_card.document.selection_view())
        if feedback is not None:
            payload["previous_errors"] = feedback
        result = await self._request_model(prompt=GENERATION_PROMPT,
            payload=payload, model=self._model_config.model_name)
        issues = result.get("issues", [])
        if not isinstance(issues, list) or any(not isinstance(issue, str) for issue in issues):
            raise InvalidStateError("生成模型issues必须为字符串数组")
        if result.get("status") == "budget_conflict":
            raise InvalidStateError("摘要预算冲突：" + "；".join(issues))
        if result.get("status") != "ok":
            raise InvalidStateError("生成模型返回未知状态")
        return StructuredSummary.from_model_data(result.get("summary"))

    def decide_context_maintenance(
        self,
        *,
        session_id: str,
        through_turn_seq: int,
        session_progress_tokens: int,
        inherited_tokens: int,
        handoff_ready: bool,
        at_complete_turn_boundary: bool,
    ) -> LifecycleDecision:
        """完整轮后80%准备、95%交接；提前READY不触发切换。

        handoff_ready必须已核对来源、切换点之前的全部连续尾部和下一窗口容量；
        仅有旧冻结点的READY摘要并不代表就绪。这里不执行调度或切换事务。
        """
        return self._lifecycle_policy.decide_context_maintenance(
            session_id=session_id, through_turn_seq=through_turn_seq,
            session_progress_tokens=session_progress_tokens, inherited_tokens=inherited_tokens,
            handoff_ready=handoff_ready, at_complete_turn_boundary=at_complete_turn_boundary,
        )

    def plan_compaction(
        self,
        *,
        session_id: str,
        frozen_through_turn_seq: int,
        turn_tokens: tuple[TurnTokenInfo, ...],
        inherited_context: InheritedContextRef | None = None,
    ) -> CompactionPlan | None:
        """继承材料与本session完整冻结原文共同生成一份交接摘要。

        调用方在触发交接后固定完整轮上界；仓储保证统计前缀连续且计数一致。
        inherited_context必须是入场时固定的引用，不能读取前驱最新版本替代。
        冻结范围全部纳入摘要，不再区分增量/rebuild，也不选择保留轮数。
        冻结后新增的原文由交接事务检查接续，不能因本计划未覆盖而删除。
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise InvalidStateError("session_id不能为空")
        if type(frozen_through_turn_seq) is not int or frozen_through_turn_seq < 0:
            raise InvalidStateError("来源上界必须是非负整数")
        if len(turn_tokens) != frozen_through_turn_seq:
            raise InvalidStateError("轮次统计没有完整覆盖固定来源前缀")

        if frozen_through_turn_seq == 0:
            return None
        if inherited_context is not None:
            self._validate_inherited(inherited_context, session_id)

        source = TurnRange(session_id, 1, frozen_through_turn_seq)
        return CompactionPlan(
            session_id=session_id,
            mode=CompactionMode.HANDOFF,
            source_turns=source,
            retained_tail=None,
            prompt_budget=self._prompt_budget,
            compaction_policy=self._compaction_policy,
            inherited_context=inherited_context,
            context_only_turns=None,
            planned_summary_tokens=self._compaction_policy.summary_target_tokens,
        )

    @staticmethod
    def _validate_inherited(ref: InheritedContextRef, session_id: str) -> None:
        """前驱必须不同，继承原文尾部必须连续接续其固定摘要。"""
        if not ref.predecessor_session_id or ref.predecessor_session_id == session_id:
            raise InvalidStateError("继承前驱不合法")
        covered = ref.summary_snapshot_ref.covered_through_turn_seq if ref.summary_snapshot_ref else 0
        raw = ref.source_range
        if raw is not None and (
            raw.session_id != ref.predecessor_session_id or raw.from_seq != covered + 1
            or raw.through_seq < covered or covered < 0
        ):
            raise InvalidStateError("继承原文必须连续接续其摘要覆盖点")

    def assess_compaction(
        self,
        *,
        summary_tokens: int,
        retries_used: int,
        information_preserved: bool,
    ) -> CompactionAssessment:
        return self._compaction_policy.assess(
            summary_tokens=summary_tokens,
            retries_used=retries_used,
            information_preserved=information_preserved,
        )

    def build_candidate_summary(
        self,
        *,
        summary: StructuredSummary,
        manifest: SummarySourceManifest,
        attempt_version: int,
        candidate_version: int,
        versions: SummaryVersions,
        token_counter: TokenCounter,
    ) -> SummaryCandidate:
        return SummaryCandidate.from_summary(
            summary=summary,
            manifest=manifest,
            attempt_version=attempt_version,
            candidate_version=candidate_version,
            versions=versions,
            token_counter=token_counter,
        )

    async def _load_model_input(
        self,
        *,
        job: ContextJob,
        repository: ContextMaintenanceRepository,
    ) -> SummaryModelInput:
        """加载本代次固定的模型正文；用于首次生成和恢复后的程序校验，不推进任务状态。"""
        plan, manifest = job.plan, job.source_manifest
        if plan is None or manifest is None or plan.source_turns is None:
            raise InvalidStateError("任务尚未固定计划和来源清单，请先保存生成恢复点")
        if (plan.session_id != job.session_id or manifest.session_id != job.session_id
                or plan.source_turns != manifest.source_turns
                or plan.source_turns.through_seq != job.frozen_through_turn_seq
                or plan.inherited_context != manifest.inherited_context):
            raise InvalidStateError("任务、计划和固定来源清单不一致")
        try:
            source = await repository.read_model_input(plan)
        except (NotFoundError, InvalidStateError) as error:
            raise ContextSourceChangedError("固定来源缺失、失效或继承材料发生变化") from error
        if source.plan != plan or source.source_manifest != manifest:
            raise ContextSourceChangedError("实际模型正文与本次尝试固定的来源清单不一致")
        return source

    def validate_candidate(
        self, source: SummaryModelInput, candidate: SummaryCandidate, token_counter: TokenCounter,
    ) -> SummaryValidation:
        """只检查可确定的结构、来源、日期和token，不调用模型判定语义。"""
        issues = []
        scope = source.plan.source_turns
        if (candidate.source_manifest != source.source_manifest or scope is None
                or source.source_manifest.source_turns != scope
                or candidate.covered_through_turn_seq != scope.through_seq):
            issues.append("候选与固定来源或覆盖点不一致")
        if candidate.versions.tokenizer_version != token_counter.tokenizer_version:
            issues.append("候选tokenizer版本不一致")
        text = candidate.summary.render()
        tokens = token_counter.count_messages((PromptMessage.reference("上一会话摘要", text),))
        if candidate.summary_text != text or candidate.content_hash != candidate.summary.content_hash():
            issues.append("候选渲染或hash不一致")
        if candidate.token_count != tokens or not 0 < tokens < source.plan.compaction_policy.summary_token_limit:
            issues.append("摘要token计数不一致或超过严格上限")
        if text == "{}":
            issues.append("摘要没有可注入的正文")
        if not candidate.summary.collect_source_refs().issubset(source.collect_allowed_source_refs()):
            issues.append("摘要引用了固定来源之外的内容")
        issues.extend(candidate.summary.validation_issues())
        return SummaryValidation(candidate.candidate_version, not issues, tuple(issues),
            "handoff-validation-v2", candidate.content_hash)
