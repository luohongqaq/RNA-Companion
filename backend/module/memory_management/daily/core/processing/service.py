"""Daily生产业务入口；生成按昂贵操作结果保存恢复点。"""
from __future__ import annotations
from collections.abc import Awaitable, Callable
from module.character.core.domain.models import CharacterCard
from module.shared.common.errors.base import InvalidStateError, NotFoundError
from module.chat.reply.core.domain.ports import ReplyPromptResourcePort
from datetime import datetime, timezone
from logger import stage
from module.memory_management.daily.core.domain.document import DailyDocument
from module.memory_management.daily.core.domain.generation import DailyCandidate
from module.memory_management.daily.core.domain.job import DailyJob
from module.memory_management.daily.core.domain.job import DailyJobInput
from module.memory_management.daily.core.domain.ports import DailyProductionRepository
from module.memory_management.daily.core.domain.publication import DailyWrite
from module.memory_management.daily.core.domain.review import DailyReview
from module.memory_management.daily.core.domain.source import DailySourceInput
from module.memory_management.daily.core.processing.config import DailyModelConfig
from module.memory_management.daily.core.processing.ports import DailyModelPort
from module.memory_management.daily.core.processing.prompts.generation import GENERATION_PROMPT
from module.memory_management.daily.core.processing.prompts.review import REVIEW_PROMPT
from module.memory_management.daily.core.domain.ports import DailyIndexPort
from module.memory_management.shared.contracts.errors import MemoryIndexUnavailable, MemoryModelUnavailable
from module.memory_management.shared.jobs.core.domain.job import MemoryJob, MemoryJobStatus, MemoryJobType
from module.memory_management.shared.jobs.core.processing.service import MemoryProductionService


class DailyProductionService(MemoryProductionService):
    job_family = "daily"

    def __init__(self, repository: DailyProductionRepository, *,
                 daily_generator: Callable[[DailySourceInput], Awaitable[DailyCandidate]] | None = None,
                 model: DailyModelPort | None = None,
                 model_config: DailyModelConfig | None = None,
                 prompt_resources: ReplyPromptResourcePort | None = None,
                 review_model: DailyModelPort | None = None,
                 review_model_config: DailyModelConfig | None = None,
                 index_repository: DailyIndexPort | None = None) -> None:
        self._repository = repository
        self._index_repository = index_repository
        self._daily_generator = daily_generator
        self._model = model
        self._model_config = DailyModelConfig() if model_config is None else model_config
        self._prompt_resources = prompt_resources
        self._review_model = review_model  # GLM生成与DeepSeek审核使用独立连接，禁止隐式复用
        self._review_model_config = DailyModelConfig.for_review() if review_model_config is None else review_model_config

    async def process_job(self, job_id: str) -> None:
        """一次推进一个持久化阶段，索引等待和终态不主动执行。"""
        job = await self._repository.get_job(job_id)
        if job is None:
            raise NotFoundError("daily任务不存在")
        if not isinstance(job, DailyJob):
            raise InvalidStateError("任务不属于daily链")
        handlers = {
            MemoryJobStatus.NEED_GENERATE: self._generate,
            MemoryJobStatus.NEED_REVIEW: self._review,
            MemoryJobStatus.NEED_WRITE: self._prepare_publication,
            MemoryJobStatus.NEED_INDEX: self._index,
            MemoryJobStatus.NEED_ACTIVATE: self._activate,
        }
        handler = handlers.get(job.status)
        if handler is not None:
            await handler(job)

    async def register_daily_jobs(self, *, limit: int = 32, priority: int = 10) -> tuple[str, ...]:
        """扫描一批封存session，按每轮第一条用户消息的后端当地日期登记daily。

        每个session单独原子登记；单项失败不阻塞其余项，未标记项下次继续扫描。
        """
        return await self._register_jobs_for_pending_sessions(limit=limit, priority=priority)

    async def _register_jobs_for_session(self, session_id: str, *, priority: int) -> tuple[str, ...]:
        source = await self._repository.read_sealed_session_source(session_id)
        by_date = source.group_daily_message_ids()
        commands = tuple(DailyJobInput.create_command(
            source.character_id, session_id, memory_date, tuple(by_date[memory_date]), priority=priority,
        ) for memory_date in sorted(by_date))
        return await self._repository.register_daily_jobs_for_session(session_id, commands)

    @stage("memory.daily.generate")
    async def _generate(self, job: MemoryJob) -> None:
        """登记代次→一次准备材料→事务外生成→原子保存生成恢复点。"""
        if not isinstance(job, DailyJob) or job.job_type != MemoryJobType.CREATE_DAILY:
            raise NotImplementedError("当前仅接入封存session的daily生成编排")
        if self._daily_generator is None and self._model is None:
            raise NotImplementedError("daily生成模型尚未接入")
        if self._daily_generator is None and self._prompt_resources is None:
            raise NotImplementedError("daily生成的角色资源读取尚未接入")
        # 登记一次开始状态 —> MemoryJobStatus.GENERATING
        # 返回的dailyjob只有状态和版本变了
        started = await self._repository.begin_generate(job.job_id)
        if started is None:
            return
        # 根据session_id和date就能完成daily任务了, 从存储层中读出所有材料组装成DailySourceInput
        source = await self._repository.read_daily_source(started)
        candidate = (await self._daily_generator(source) if self._daily_generator is not None
                     else await self.generate_daily(source, feedback=job.last_error))
        await self._repository.save_candidate(started.job_id, started.attempt_version, candidate, source=source)

    async def generate_daily(self, source: DailySourceInput, *,
                             character_card: CharacterCard | None = None,
                             feedback: str | None = None) -> DailyCandidate:
        """使用提取提示词生成完整daily，程序校验后构造候选；调用期间不持有事务。"""
        if self._model is None:
            raise NotImplementedError("daily生成模型尚未接入")
        if character_card is None:
            if self._prompt_resources is None:
                raise NotImplementedError("daily生成的角色资源读取尚未接入")
            resources = await self._prompt_resources.load(source.plan.character_id)
            character_card = resources.character_card
        if character_card.character_id != source.plan.character_id:
            raise InvalidStateError("daily角色卡与来源角色不一致")
        config = self._model_config
        payload = source.to_model_payload(character_context=character_card.document.selection_view())
        if feedback is not None:
            payload["PREVIOUS_ERRORS"] = feedback
        result = await self._model.complete(GENERATION_PROMPT, payload, config)
        try:
            sources = (*(source.base_daily.source_session_ids if source.base_daily is not None else ()),
                       source.plan.session_id)
            document = DailyDocument.from_model_data(result, session_id=source.plan.session_id,
                memory_date=source.plan.memory_date, source_session_ids=tuple(dict.fromkeys(sources)),
                allowed_turn_ids=source.allowed_source_turn_ids())
            if not document.has_memories and source.base_daily is not None and source.base_daily.has_memories:
                raise InvalidStateError("本次无新增记忆不能清空当天已有日记")
        except InvalidStateError as error:
            raise MemoryModelUnavailable(str(error), retryable=True) from error
        return DailyCandidate(candidate_version=1, document=document,
                              content_hash=document.content_hash(), generated_at=datetime.now(timezone.utc))



    @stage("memory.daily.review")
    async def _review(self, job: MemoryJob) -> None:
        """保存生成结果后独立开始审核；只有模型结果形成新恢复点。"""
        if not isinstance(job, DailyJob) or job.job_type != MemoryJobType.CREATE_DAILY:
            raise NotImplementedError("当前仅实现daily审核")
        if self._review_model is None or self._prompt_resources is None:
            raise NotImplementedError("daily审核模型或角色资源尚未接入")
        started = await self._repository.begin_review(job.job_id)
        if started is None:
            return
        source = await self._repository.read_daily_source(started)
        review = await self.review_daily(source, started.candidate)
        await self._repository.save_review(started.job_id, started.attempt_version, review,
            retry_rejected=started.reextraction_count < self._review_model_config.max_reextractions)

    async def review_daily(self, source: DailySourceInput, candidate: DailyCandidate) -> DailyReview:
        """固定来源审核，允许有理由的栏目删减，不把主观反思当成外部事实。"""
        if self._review_model is None or self._prompt_resources is None:
            raise NotImplementedError("daily审核模型或角色资源尚未接入")
        resources = await self._prompt_resources.load(source.plan.character_id)
        if resources.character_card.character_id != source.plan.character_id:
            raise InvalidStateError("daily审核角色卡与来源角色不一致")
        payload = source.to_model_payload(character_context=resources.character_card.document.selection_view())
        payload["CANDIDATE"] = candidate.document.to_data()
        result = await self._review_model.complete(REVIEW_PROMPT, payload, self._review_model_config)
        try:
            review = DailyReview.from_model_data(result, candidate=candidate.document,
                session_id=source.plan.session_id, allowed_turn_ids=source.allowed_source_turn_ids())
            return review
        except InvalidStateError as error:
            raise MemoryModelUnavailable(str(error), retryable=True) from error

    async def _revise(self, job: MemoryJob) -> None:
        """根据已保存问题修订候选，不读取额外的新来源。"""
        raise NotImplementedError("待实现候选修订")

    @stage("memory.daily.write")
    async def _prepare_publication(self, job: MemoryJob) -> None:
        """读取审核结果→事务外渲染→保存待发布正文；不调用模型。"""
        if not isinstance(job, DailyJob) or job.job_type != MemoryJobType.CREATE_DAILY:
            raise NotImplementedError("当前仅实现daily正文准备")
        started = await self._repository.begin_write(job.job_id)
        if started is None:
            return
        result = DailyWrite.from_document(started.final_document())
        await self._repository.save_write(started.job_id, started.attempt_version, result)

    @stage("memory.daily.index")
    async def _index(self, job: MemoryJob) -> None:
        """SQLite登记开始→Qdrant编码并批量入库→SQLite登记完成。"""
        if not isinstance(job, DailyJob):
            raise NotImplementedError("当前仅实现daily分区索引")
        if self._index_repository is None:
            raise NotImplementedError("记忆向量持久层尚未接入")
        started = await self._repository.begin_index(job.job_id)
        if started is None:
            return
        document = started.index_document()
        representations = started.write_result.index_representations(
            document.ref, started.input.character_id, started.input.recall_tier)
        if not await self._index_repository.batch_hybrid_index(document, representations):
            raise MemoryIndexUnavailable("记忆索引尚未确认完成")
        await self._repository.save_index(started.job_id, started.attempt_version, document)

    @stage("memory.daily.activate")
    async def _activate(self, job: MemoryJob) -> None:
        """索引确认后在SQLite事务内激活正式JSON并完成任务。"""
        if not isinstance(job, DailyJob):
            raise NotImplementedError("当前只实现daily发布")
        await self._repository.activate_revision(job.job_id, job.attempt_version)
