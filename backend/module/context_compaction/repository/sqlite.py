"""上下文任务登记、单协程调度、程序校验保存与session交接。"""
from logger import stage
from datetime import datetime, timezone
from collections import defaultdict
from dataclasses import asdict, replace
import hashlib
import json

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from module.chat.conversation.core.domain.models import ConversationState, Session, SessionState, TurnState
from module.shared.context.types.turns import TurnRange
from module.shared.context.types.messages import CompletedTurnResult
from module.shared.common.errors.base import InvalidStateError, IdempotencyConflictError, NotFoundError
from module.shared.common.ids import IdGenerator
from module.context_compaction.core.domain.job import ContextJob, CreateContextJobCommand, ContextJobType, ContextJobStatus
from module.context_compaction.core.domain.source import SummaryModelInput
from module.shared.context.types.working import InheritedContext
from module.context_compaction.core.domain.lifecycle_policy import SessionLifecycleConfig
from module.context_compaction.core.domain.plan import CompactionPlan, TurnTokenInfo
from module.context_compaction.core.domain.references import CompactionMode
from module.shared.context.types.references import ContextSourceRef, InheritedContextRef, SnapshotRef
from module.context_compaction.core.domain.snapshots import ContextSnapshot, SnapShotState, SummarySourceManifest
from module.context_compaction.core.domain.candidate import SummaryCandidate, SummaryValidation
from module.context_compaction.repository import mappers as context_mappers
from storage.sqlite.value_codecs import SQLiteValueCodec
from module.chat.conversation.repository import mappers as conversation_mappers
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import ContextJobRow, SessionRow, ConversationRow, TurnRow, ContextSnapshotRow, MessageRow


class SQLiteContextMaintenanceRepository:
    """一个 session 只发布一份交接摘要"""
    def __init__(self, database: SQLiteDatabase, *, lifecycle_policy: SessionLifecycleConfig | None = None) -> None:
        self._database = database
        self._lifecycle_policy = SessionLifecycleConfig() if lifecycle_policy is None else lifecycle_policy

    async def create_job(self, command: CreateContextJobCommand) -> str:
        """独立事务登记上下文维护任务，重复请求返回原 job_id。

        同 dedupe_key 已存在且登记参数不同时报幂等冲突；用于测试与显式登记。
        需要嵌入调用方事务时（如T3采用事务）使用 create_job_in_transaction。
        输入:
            CreateContextJobCommand:
            {job_type, session_id, frozen_through_turn_seq,
             priority, dedupe_key}
        输出:
            已提交的 context_jobs.job_id。
        """
        self._validate_create_command(command)
        async with self._database.transaction() as session:
            existing_row = await session.scalar(
                select(ContextJobRow).where(
                    ContextJobRow.dedupe_key == command.dedupe_key,
                )
            )
            if existing_row is not None:
                # 同一 dedupe_key 对应的登记参数不一致应报幂等冲突，一致才返回已有任务。
                existing_input = (
                    existing_row.job_type,
                    existing_row.session_id,
                    existing_row.payload_json["frozen_through_turn_seq"],
                    existing_row.priority,
                )
                requested_input = (
                    command.job_type.value,
                    command.session_id,
                    command.frozen_through_turn_seq,
                    command.priority,
                )
                if existing_input != requested_input:
                    raise IdempotencyConflictError("dedupe_key 已被其他任务输入使用")
                # 已有记录只返回原登记结果，不再次插入任务。
                return existing_row.job_id
            # 来源前缀预检仅独立入口执行；T3登记允许含中断轮，生成时再验。
            await self._validate_source_prefix(session, command)
            return await self._register_new_job(session, command)

    async def prepare_handoff(self, session_id: str, through_turn_seq: int) -> str:
        """登记交接摘要任务；沿用同一边界原任务，上次失败的任务重新排入待生成。"""
        async with self._database.transaction() as session:
            existing = await session.scalar(select(ContextJobRow).where(
                ContextJobRow.dedupe_key == f"prepare_handoff:{session_id}",
            ))
            if existing is not None:
                if existing.status == ContextJobStatus.FAILED.value:
                    await self._revive_handoff(session, existing)
                return existing.job_id
        command = CreateContextJobCommand(
            job_type=ContextJobType.PREPARE_HANDOFF,
            session_id=session_id,
            frozen_through_turn_seq=through_turn_seq,
            priority=10,
            dedupe_key=f"prepare_handoff:{session_id}",
        )
        try:
            return await self.create_job(command)
        except IdempotencyConflictError:
            return ""

    async def handoff_status(self, session_id: str) -> str | None:
        """读取交接任务状态；没有登记时返回 None。"""
        async with self._database.db_session() as session:
            return await session.scalar(select(ContextJobRow.status).where(
                ContextJobRow.dedupe_key == f"prepare_handoff:{session_id}",
            ))

    @staticmethod
    async def _revive_handoff(session: AsyncSession, row: ContextJobRow) -> None:
        """失败任务重新排入待生成；重试次数清零，执行代次前移让旧结果失效。"""
        job = context_mappers.context_job_to_domain(row)
        now = datetime.now(timezone.utc)
        job = replace(job, status=ContextJobStatus.NEED_GENERATE, retry_count=0, last_error=None,
                      available_at=now, updated_at=now, finished_at=None,
                      attempt_version=job.attempt_version + 1)
        row.payload_json = context_mappers.context_job_payload_to_json(job)
        row.status, row.updated_at = job.status.value, now
        await session.flush()

    async def create_job_in_transaction(self, session: AsyncSession, command: CreateContextJobCommand) -> str:
        """嵌入调用方事务的登记核心；dedupe命中返回原任务，不追新上界。

        交接任务沿用首次冻结的来源边界，不因后续触发追到最新轮次；
        与 commit_handoff_in_transaction 同为跨模块事务内入口，不进入业务Protocol。
        """
        self._validate_create_command(command)
        existing_row = await session.scalar(
            select(ContextJobRow).where(
                ContextJobRow.dedupe_key == command.dedupe_key,
            )
        )
        if existing_row is not None:
            return existing_row.job_id
        return await self._register_new_job(session, command)

    @staticmethod
    def _validate_create_command(command: CreateContextJobCommand) -> None:
        if not isinstance(command.job_type, ContextJobType):
            raise InvalidStateError("不支持的上下文任务类型")
        if not isinstance(command.session_id, str) or not command.session_id.strip():
            raise InvalidStateError("session_id 不能为空")
        if not isinstance(command.dedupe_key, str) or not command.dedupe_key.strip():
            raise InvalidStateError("dedupe_key 不能为空")
        if type(command.frozen_through_turn_seq) is not int or command.frozen_through_turn_seq < 1:
            raise InvalidStateError("来源上界必须是正整数")
        if type(command.priority) is not int:
            raise InvalidStateError("priority 必须是整数")

    @staticmethod
    async def _validate_source_prefix(session: AsyncSession, command: CreateContextJobCommand) -> None:
        """独立登记入口的前缀预检；(session_id, turn_seq) 唯一时1~N有N个完整轮即连续。"""
        complete_count = await session.scalar(
            select(func.count(TurnRow.turn_id)).where(
                TurnRow.session_id == command.session_id,
                TurnRow.turn_seq.between(1, command.frozen_through_turn_seq),
                TurnRow.turn_state == TurnState.COMPLETE.value,
                TurnRow.accepted_message_id.is_not(None),
            )
        )
        if complete_count != command.frozen_through_turn_seq:
            raise InvalidStateError("来源前缀存在缺失轮次或尚未完成的轮次")

    async def _register_new_job(self, session: AsyncSession, command: CreateContextJobCommand) -> str:
        """校验路由后插入新任务；由两个登记入口在各自事务内调用。"""
        session_row = await session.get(SessionRow, command.session_id)
        if session_row is None:
            raise NotFoundError("session 不存在")
        conversation_row = await session.get(ConversationRow, session_row.conversation_id)
        if conversation_row is None:
            raise InvalidStateError("session 缺少所属 conversation")
        if (
                conversation_row.state != ConversationState.ACTIVE.value
                or session_row.session_state not in (SessionState.OPEN.value, SessionState.WAITING_HANDOFF.value)
                or conversation_row.current_session_id != session_row.session_id
        ):
            raise InvalidStateError("只能为当前活动 session 登记上下文维护任务")
        now = datetime.now(timezone.utc)
        job_id = IdGenerator.new("context_job")
        job = ContextJob(
            job_id=job_id,
            job_type=command.job_type,
            session_id=command.session_id,
            target_key=f"session:{command.session_id}",
            dedupe_key=command.dedupe_key,
            priority=command.priority,
            status=ContextJobStatus.NEED_GENERATE,
            available_at=now,
            frozen_through_turn_seq=command.frozen_through_turn_seq,
            created_at=now,
            updated_at=now,
        )
        session.add(context_mappers.context_job_to_row(job))
        await session.flush()
        return job_id

    async def get_job(self, job_id: str) -> ContextJob | None:
        """按任务 ID 读取任务及已持久化产物，不推进状态。"""
        async with self._database.db_session() as session:
            job_row = await session.get(ContextJobRow, job_id)
            if job_row is None:
                return None
            return context_mappers.context_job_to_domain(job_row)

    async def get_next_executable_job(self) -> ContextJob | None:
        """为唯一上下文维护协程读取下一个到期任务，不修改数据库。

        同 session、同类型只调度最早的非终态任务，退避或READY等待采用的队首不能被越过。
        不同目标之间按 priority、created_at、job_id 排序。
        调用方必须 await 当前处理结束后再取下一个，不能并发派发。调用方必须串行地“取一个 → 做完 → 再取下一个”
        """
        active_statuses = (
            ContextJobStatus.NEED_GENERATE.value,
            ContextJobStatus.GENERATING.value,
            ContextJobStatus.NEED_VALIDATE.value,
            ContextJobStatus.VALIDATING.value,
            ContextJobStatus.NEED_PUBLISH.value,
        )
        # READY等待交接，不再执行生成阶段；仍占据同一目标的队首位置。
        blocking_statuses = (*active_statuses, ContextJobStatus.READY.value)
        earlier = aliased(ContextJobRow)
        # 前面的任务即使还没到重试时间，也要等它处理完，不能先处理后面的任务。
        has_earlier = select(earlier.job_id).where(
            earlier.session_id == ContextJobRow.session_id,
            earlier.job_type == ContextJobRow.job_type,
            earlier.status.in_(blocking_statuses),
            or_(
                earlier.created_at < ContextJobRow.created_at,
                and_(
                    earlier.created_at == ContextJobRow.created_at,
                    earlier.job_id < ContextJobRow.job_id,
                ),
            ),
        ).correlate(ContextJobRow).exists()
        statement = select(ContextJobRow).where(
            ContextJobRow.status.in_(active_statuses),
            ContextJobRow.available_at <= datetime.now(timezone.utc),
            ~has_earlier,
        ).order_by(
            ContextJobRow.priority, ContextJobRow.created_at, ContextJobRow.job_id,
        ).limit(1)

        async with self._database.db_session() as session:
            row = await session.scalar(statement)
            return context_mappers.context_job_to_domain(row) if row is not None else None

    async def read_current_snapshot(self, session_id: str) -> ContextSnapshot | None:
        """读取指定session唯一已采用的VALID交接摘要，不读取READY。

        按 session_id 筛选而不是按快照主键查询；不要求 session 仍然 open，
        以支持读取封存前驱的摘要。缺失的来源清单由 mapper 明确报错。
        """
        async with self._database.db_session() as session:
            if await session.get(SessionRow, session_id) is None:
                raise NotFoundError("session 不存在")
            row = await session.scalar(select(ContextSnapshotRow).where(
                ContextSnapshotRow.session_id == session_id,
                ContextSnapshotRow.snapshot_state == SnapShotState.VALID.value,
            ))
            return context_mappers.snapshot_to_domain(row) if row is not None else None

    async def read_inherited_context(self, session_id: str) -> InheritedContextRef | None:
        """只读取交接事务保存的引用，不查询前驱最新摘要或重新计算尾部。"""
        async with self._database.db_session() as session:
            row = await session.get(SessionRow, session_id)
            if row is None:
                raise NotFoundError("session不存在")
            return self._inherited_context_from_session(row)

    @staticmethod
    def _inherited_context_from_session(row: SessionRow) -> InheritedContextRef | None:
        """固定引用须与session前驱对应，后继窗口不能把缺失引用当作首窗口。"""
        inherited = SQLiteValueCodec.inherited_context_from_json(row.inherited_context_json)
        if row.predecessor_session_id is None:
            if inherited is not None:
                raise InvalidStateError("首窗口不应保存前驱继承材料")
        elif inherited is None or inherited.predecessor_session_id != row.predecessor_session_id:
            raise InvalidStateError("后继session缺少固定继承引用或前驱不一致")
        return inherited

    async def read_model_input(self, plan: CompactionPlan) -> SummaryModelInput:
        """同一短读事务读取完整模型材料和实际清单，避免来源核对与正文读取之间出现变化。"""
        async with self._database.db_session() as session:
            return await self._read_model_input(session, plan)

    async def read_turn_token_infos(
        self, session_id: str, through_turn_seq: int,
    ) -> tuple[TurnTokenInfo, ...]:
        """读取当前 session 第 1 轮至固定上界的原文 token 统计。

        不先过滤未完成轮，避免将缺失或未完成的来源静默丢弃。
        不读上界之后的新轮次，不计入前驱 session 的继承原文。
        """
        if type(through_turn_seq) is not int or through_turn_seq < 0:
            raise InvalidStateError("来源上界必须是非负整数")

        async with self._database.db_session() as session:
            if await session.get(SessionRow, session_id) is None:
                raise NotFoundError("session 不存在")
            if through_turn_seq == 0:
                return ()

            rows = (await session.scalars(select(TurnRow).where(
                TurnRow.session_id == session_id,
                TurnRow.turn_seq.between(1, through_turn_seq),
            ).order_by(TurnRow.turn_seq))).all()
            if len(rows) != through_turn_seq:
                raise InvalidStateError("来源前缀存在缺失轮次")

            cumulative = 0
            tokenizer_version = None
            for expected_seq, row in enumerate(rows, start=1):
                if (row.turn_seq != expected_seq
                        or row.turn_state != TurnState.COMPLETE.value
                        or row.accepted_message_id is None):
                    raise InvalidStateError("来源前缀不连续或包含未完成轮次")
                if (row.turn_token_count < 0 or row.cumulative_token_count < 0
                        or row.tokenizer_version is None or not row.tokenizer_version.strip()):
                    raise InvalidStateError("完整轮缺少可用的 token 统计")
                if tokenizer_version is None:
                    tokenizer_version = row.tokenizer_version
                elif row.tokenizer_version != tokenizer_version:
                    raise InvalidStateError("来源前缀的 tokenizer 版本不一致，需要重新统计")
                cumulative += row.turn_token_count
                if row.cumulative_token_count != cumulative:
                    raise InvalidStateError("原文累计 token 统计不一致")

            return tuple(context_mappers.turn_token_info_from_row(row) for row in rows)

    async def _read_source_manifest(self, session: AsyncSession, plan: CompactionPlan) -> SummarySourceManifest:
        """固定来源与后续正文加载共用同一套读取和hash规则，不写任务。"""
        return (await self._read_model_input(session, plan)).source_manifest

    async def _read_model_input(self, session: AsyncSession, plan: CompactionPlan) -> SummaryModelInput:
        """一次读取组装实际正文及来源清单，hash不含未采用回复和冻结上界后的消息。"""
        scope = plan.source_turns
        if (plan.mode != CompactionMode.HANDOFF or scope is None
                or scope.session_id != plan.session_id or scope.from_seq != 1 or scope.through_seq < 1
                or plan.retained_tail is not None or plan.context_only_turns is not None):
            raise InvalidStateError("交接计划必须包含当前session的完整原文前缀")
        current = await session.get(SessionRow, plan.session_id)
        if current is None:
            raise NotFoundError("session不存在")
        conversation = await session.get(ConversationRow, current.conversation_id)
        if conversation is None:
            raise NotFoundError("session所属conversation不存在")
        refs, turns, owned_turns = await self._read_manifest_turns(session, scope)
        inherited = plan.inherited_context
        if inherited != self._inherited_context_from_session(current):
            raise InvalidStateError("计划继承引用与session保存的固定引用不一致")
        inherited_payload = None
        inherited_context = None
        if inherited is not None:
            if inherited.predecessor_session_id != current.predecessor_session_id:
                raise InvalidStateError("继承引用与session前驱不一致")
            predecessor = await session.get(SessionRow, inherited.predecessor_session_id)
            if predecessor is None or predecessor.conversation_id != current.conversation_id:
                raise InvalidStateError("继承前驱不存在或不属于当前对话")
            snapshot_payload = None
            snapshot_context = None
            snapshot_ref = inherited.summary_snapshot_ref
            if snapshot_ref is not None:
                row = await session.get(ContextSnapshotRow, snapshot_ref.snapshot_id)
                if (row is None or row.session_id != predecessor.session_id
                        or context_mappers.snapshot_to_ref(row) != snapshot_ref
                        or row.snapshot_state != SnapShotState.VALID.value):
                    raise InvalidStateError("固定继承摘要缺失、未采用或引用不匹配")
                snapshot_payload = {"ref": asdict(snapshot_ref), "summary": row.summary_json,
                                    "text": row.rendered_summary_text}
                snapshot_context = context_mappers.snapshot_to_context(row)
            covered = snapshot_ref.covered_through_turn_seq if snapshot_ref else 0
            tail = inherited.source_range
            inherited_turns = []
            inherited_complete_turns = ()
            if tail is not None:
                if (tail.session_id != predecessor.session_id or tail.from_seq != covered + 1
                        or tail.through_seq < covered):
                    raise InvalidStateError("继承原文未连续接续固定摘要")
                if tail.from_seq <= tail.through_seq:
                    tail_refs, inherited_turns, inherited_complete_turns = await self._read_manifest_turns(session, tail)
                    refs += tail_refs
            inherited_payload = {"ref": asdict(inherited), "snapshot": snapshot_payload,
                                 "turns": inherited_turns}
            inherited_context = InheritedContext(inherited, snapshot_context, inherited_complete_turns)
        payload = {"format": "handoff-source-v1", "session_id": plan.session_id,
                   "character_id": conversation.character_id,
                   "source_turns": asdict(scope), "turns": turns,
                   "inherited": inherited_payload}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        manifest = SummarySourceManifest(
            session_id=plan.session_id, source_turns=scope, source_refs=tuple(refs),
            source_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            inherited_context=inherited, inherited_context_included=inherited is not None,
        )
        return SummaryModelInput(plan=plan, source_manifest=manifest, owned_turns=owned_turns,
            inherited_context=inherited_context, character_id=conversation.character_id)

    async def _read_manifest_turns(
        self, session: AsyncSession, scope: TurnRange,
    ) -> tuple[list[ContextSourceRef], list[dict], tuple[CompletedTurnResult, ...]]:
        """读取连续完整轮，复用现有mapper选出用户消息与正式回复。"""
        rows = (await session.scalars(select(TurnRow).where(
            TurnRow.session_id == scope.session_id,
            TurnRow.turn_seq.between(scope.from_seq, scope.through_seq),
        ).order_by(TurnRow.turn_seq))).all()
        if len(rows) != scope.through_seq - scope.from_seq + 1:
            raise InvalidStateError("来源原文存在缺失轮次")
        messages = (await session.scalars(select(MessageRow).join(
            TurnRow, MessageRow.turn_id == TurnRow.turn_id,
        ).where(TurnRow.session_id == scope.session_id,
                TurnRow.turn_seq.between(scope.from_seq, scope.through_seq))
            .order_by(TurnRow.turn_seq, MessageRow.message_seq))).all()
        grouped = defaultdict(list)
        for message in messages:
            grouped[message.turn_id].append(message)
        refs, payload, complete_turns = [], [], []
        for row in rows:
            turn = conversation_mappers.completed_turn_to_result(row, grouped[row.turn_id])
            complete_turns.append(turn)
            ref = ContextSourceRef(row.turn_id, row.content_revision, row.session_id)
            refs.append(ref)
            payload.append({"ref": asdict(ref), "turn_seq": row.turn_seq, "messages": [
                {"message_id": item.message_id, "message_seq": item.message_seq,
                 "role": item.role, "content": item.content, "visual": conversation_mappers.visual_to_json(item.visual),
                 "created_at": item.created_at.isoformat()}
                for item in (*turn.user_messages, turn.accepted_reply)
            ]})
        return refs, payload, tuple(complete_turns)

    async def begin_generate(self, job_id: str) -> ContextJob | None:
        """把任务改成生成中，不读取原文或保存计划。"""
        async with self._database.transaction() as session:
            row = await session.get(ContextJobRow, job_id)
            if row is None:
                raise NotFoundError("上下文任务不存在")
            job = context_mappers.context_job_to_domain(row)
            now = datetime.now(timezone.utc)
            # 检查任务状态和执行时间：不是待生成，或者还没到重试时间，就直接返回。
            if job.status != ContextJobStatus.NEED_GENERATE or job.available_at > now:
                return None
            if not await self._can_start(session, row):
                return None
            # 更新任务状态，need_generate -> generating，同时把attempt_version加1。
            # 后面保存结果时要带上这个版本号，版本对不上就不能保存；attempt_count记录开始生成的次数。
            # 这里只记录开始生成，计划和来源等模型生成成功后，再和候选一起保存。
            job = replace(job, plan=None, source_manifest=None, candidate=None, validation=None,
                attempt_version=job.attempt_version + 1, attempt_count=job.attempt_count + 1,
                status=ContextJobStatus.GENERATING, updated_at=now, last_error=None, finished_at=None)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = context_mappers.context_job_payload_to_json(job)
            await session.flush()
            return job

    async def _can_start(self, session: AsyncSession, row: ContextJobRow) -> bool:
        """检查session是否还在使用，以及前面有没有没处理完的同类任务。"""
        current = await session.get(SessionRow, row.session_id)
        if current is None:
            raise NotFoundError("session不存在")
        conversation = await session.get(ConversationRow, current.conversation_id)
        # 检查session：已经封存，或者conversation已经切到别的session，就不能继续处理。
        if (current.session_state not in (SessionState.OPEN.value, SessionState.WAITING_HANDOFF.value)
                or conversation is None or conversation.state != ConversationState.ACTIVE.value
                or conversation.current_session_id != current.session_id):
            return False
        # 找同session、同类型的更早任务，前面的任务还没结束，就不能先做当前任务。
        # READY还在等待切换session，也算没结束；等待重试的任务同样不能跳过。
        earlier = await session.scalar(select(ContextJobRow.job_id).where(
            ContextJobRow.session_id == row.session_id, ContextJobRow.job_type == row.job_type,
            ContextJobRow.status.not_in((ContextJobStatus.DONE.value, ContextJobStatus.FAILED.value,
                                        ContextJobStatus.OBSOLETE.value)),
            or_(ContextJobRow.created_at < row.created_at,
                and_(ContextJobRow.created_at == row.created_at, ContextJobRow.job_id < row.job_id)),
        ).limit(1))
        return earlier is None

    async def begin_validate(self, job_id: str) -> ContextJob | None:
        """读取已经保存的候选，把任务改成校验中，不重新生成摘要。"""
        async with self._database.transaction() as session:
            row = await session.get(ContextJobRow, job_id)
            if row is None:
                raise NotFoundError("上下文任务不存在")
            job = context_mappers.context_job_to_domain(row)
            now = datetime.now(timezone.utc)
            if job.status != ContextJobStatus.NEED_VALIDATE or job.available_at > now:
                return None
            if job.candidate is None or job.plan is None or job.source_manifest is None:
                raise InvalidStateError("待校验任务缺少生成恢复点")
            if not await self._can_start(session, row):
                return None
            # 更新任务状态，need_validate -> validating，attempt_version加1，表示开始了新的校验。
            # 保留已经生成的候选；这里没有调用生成模型，所以不增加attempt_count。
            # candidate.attempt_version记录它生成时的版本号，这里不修改。
            job = replace(job, status=ContextJobStatus.VALIDATING,
                attempt_version=job.attempt_version + 1, last_error=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = context_mappers.context_job_payload_to_json(job)
            await session.flush()
            return job

    async def save_candidate(self, job_id: str, attempt_version: int, candidate: SummaryCandidate,
                             *, source: SummaryModelInput) -> bool:
        """同一事务保存候选并进入程序校验阶段；迟到结果不覆盖当前任务。"""
        async with self._database.transaction() as session:
            row = await session.get(ContextJobRow, job_id)
            if row is None:
                raise NotFoundError("上下文任务不存在")
            job = context_mappers.context_job_to_domain(row)
            # CAS检查：版本对不上，说明任务已经重新开始过，这次返回的生成结果直接拒绝。
            if job.attempt_version != attempt_version or candidate.attempt_version != attempt_version:
                return False
            if job.status != ContextJobStatus.GENERATING:
                # CAS检查：同版本的候选已经保存过，内容相同就返回True，不重复保存或修改后面的状态。
                return job.status in (
                    ContextJobStatus.NEED_VALIDATE, ContextJobStatus.VALIDATING,
                    ContextJobStatus.NEED_PUBLISH,
                    ContextJobStatus.READY,
                    ContextJobStatus.DONE,
                ) and job.candidate == candidate and job.plan == source.plan and job.source_manifest == source.source_manifest
            # 装填检查：计划、来源清单和候选必须属于这个session，压缩到哪一轮也要和任务登记的一致。
            # 这里不再读取整份聊天，后面的校验会重新读取原文并比较hash，检查聊天内容有没有被修改。
            plan, manifest = source.plan, source.source_manifest
            if (plan.session_id != job.session_id or plan.source_turns is None
                    or plan.source_turns.through_seq != job.frozen_through_turn_seq
                    or manifest.session_id != job.session_id or manifest.source_turns != plan.source_turns
                    or manifest.inherited_context != plan.inherited_context
                    or candidate.source_manifest != manifest
                    or candidate.covered_through_turn_seq != job.frozen_through_turn_seq):
                raise InvalidStateError("候选来源、计划或覆盖点与任务不一致")

            now = datetime.now(timezone.utc)
            job = replace(
                # 更新任务状态，generating -> need_validate，计划、来源清单和候选一起保存。
                job, plan=plan, source_manifest=manifest, candidate=candidate, status=ContextJobStatus.NEED_VALIDATE,
                updated_at=now, available_at=now,
            )
            row.payload_json = context_mappers.context_job_payload_to_json(job)
            row.status = job.status.value
            row.updated_at = now
            row.available_at = now
            await session.flush()
            return True

    async def save_validation(self, job_id: str, attempt_version: int, validation: SummaryValidation) -> bool:
        """保存当前候选的校验结果，通过后等待发布，不通过则重新生成或结束任务。"""
        async with self._database.transaction() as session:
            row = await session.get(ContextJobRow, job_id)
            if row is None:
                raise NotFoundError("上下文任务不存在")
            job = context_mappers.context_job_to_domain(row)
            if job.attempt_version != attempt_version:
                return False
            # CAS检查：同版本的校验结果已经保存过，内容相同就返回True，不重复增加重试次数。
            if job.status != ContextJobStatus.VALIDATING:
                return job.status in (
                    ContextJobStatus.NEED_PUBLISH, ContextJobStatus.READY, ContextJobStatus.DONE,
                    ContextJobStatus.NEED_GENERATE, ContextJobStatus.FAILED,
                ) and job.validation == validation
            candidate = job.candidate
            if candidate is None or job.plan is None:
                raise InvalidStateError("任务缺少本代次候选或压缩计划")
            # 装填检查：校验结果的candidate_version和hash必须与候选一致，确认检查的是这份候选。
            # 开始校验时任务的attempt_version已经加1，不能再要求它等于候选生成时的版本号。
            if (validation.candidate_version != candidate.candidate_version
                    or validation.candidate_hash != candidate.content_hash):
                raise InvalidStateError("校验报告与本代次候选的版本或hash不一致")
            if validation.passed and not 0 < candidate.token_count < job.plan.compaction_policy.summary_token_limit:
                raise InvalidStateError("校验通过的候选token数必须大于0且小于摘要上限")
            now = datetime.now(timezone.utc)
            if validation.passed:
                status, error, finished_at = ContextJobStatus.NEED_PUBLISH, None, None
            else:
                # 检查还能不能重新生成：attempt_count包含第一次生成，max_compression_retries只算额外次数。
                # 先保留这次校验结果，方便查看失败原因；下次begin_generate会清空它并重新生成。
                can_retry = job.attempt_count <= job.plan.compaction_policy.max_compression_retries
                status = ContextJobStatus.NEED_GENERATE if can_retry else ContextJobStatus.FAILED
                error = "; ".join(validation.issues) or "摘要程序校验未通过"
                finished_at = None if can_retry else now
            job = replace(job, validation=validation, status=status, last_error=error, finished_at=finished_at,
                retry_count=job.retry_count + (0 if validation.passed else 1), updated_at=now, available_at=now)
            row.payload_json = context_mappers.context_job_payload_to_json(job)
            row.status, row.updated_at, row.available_at = job.status.value, now, now
            row.retry_count = job.retry_count
            await session.flush()
            return True

    async def publish_ready_snapshot(self, job_id: str, attempt_version: int) -> SnapshotRef | None:
        """把校验通过的候选保存成READY快照；已经发布过就返回原来的快照引用。"""
        async with self._database.transaction() as session:
            row = await session.get(ContextJobRow, job_id)
            if row is None:
                raise NotFoundError("上下文任务不存在")
            job = context_mappers.context_job_to_domain(row)
            if job.attempt_version != attempt_version:
                return None
            # CAS检查：已经发布成功就返回原来的快照引用，不再创建一份快照。
            if job.status in (ContextJobStatus.READY, ContextJobStatus.DONE):
                return job.published_snapshot
            if job.status != ContextJobStatus.NEED_PUBLISH:
                return None
            # 装填检查：必须是这份候选通过了校验，来源清单要一致，token数也必须满足上限。
            candidate, validation = job.candidate, job.validation
            if (candidate is None or validation is None or not validation.passed
                    or validation.candidate_version != candidate.candidate_version
                    or validation.candidate_hash != candidate.content_hash
                    or job.plan is None or candidate.source_manifest != job.source_manifest
                    or not 0 < candidate.token_count < job.plan.compaction_policy.summary_token_limit):
                raise InvalidStateError("发布需要当前候选的通过校验结果")
            now = datetime.now(timezone.utc)
            snapshot = ContextSnapshot(
                snapshot_id=IdGenerator.new("snapshot"),
                session_id=job.session_id,
                state=SnapShotState.READY,
                covered_through_turn_seq=candidate.covered_through_turn_seq,
                summary=candidate.summary,
                summary_text=candidate.summary_text,
                token_count=candidate.token_count,
                content_hash=candidate.content_hash,
                versions=candidate.versions,
                source_manifest=candidate.source_manifest,
                created_at=now,
            )
            snapshot_row = context_mappers.snapshot_to_row(snapshot, producer_job_id=job.job_id)
            session.add(snapshot_row)
            ref = context_mappers.snapshot_to_ref(snapshot_row)
            # 快照与任务一起提交；READY只表示材料就绪，交接成功后才填写完成时间。
            job = replace(job, published_snapshot=ref, status=ContextJobStatus.READY,
                updated_at=now, finished_at=None)
            row.payload_json = context_mappers.context_job_payload_to_json(job)
            row.status = job.status.value
            row.updated_at = now
            await session.flush()
            # 80%提前发布时只停留READY；95%已等待交接时，在同一事务尝试切换。
            await self.commit_handoff_in_transaction(session, session_id=job.session_id)
            return ref


    @stage("context.handoff")
    async def commit_handoff(self, session_id: str) -> Session | None:
        """独立事务交接入口；重复提交返回同一个后继，边界不满足时返回None。"""
        async with self._database.transaction() as session:
            return await self.commit_handoff_in_transaction(session, session_id)

    @stage("context.handoff_degraded")
    async def commit_degraded_handoff(self, session_id: str) -> Session | None:
        """摘要反复失败时的兜底交接：不要摘要，只把冻结点之后的原文交给后继会话。"""
        async with self._database.transaction() as session:
            return await self.commit_handoff_degraded_in_transaction(session, session_id)

    async def commit_handoff_degraded_in_transaction(
        self, session: AsyncSession, session_id: str,
    ) -> Session | None:
        """兜底路径：交接摘要始终不合格时，宁可丢掉摘要也要让窗口换掉。

        冻结点（生成摘要时定格的轮数）之前的原文不再携带，冻结点之后的完整轮作为
        继承原文交给新会话；新会话没有摘要，人设与长期记忆仍按角色维度生效。
        """
        successor = await session.scalar(select(SessionRow).where(
            SessionRow.auto_successor_of == session_id,
        ))
        if successor is not None:
            return conversation_mappers.session_to_domain(successor)
        old = await session.get(SessionRow, session_id)
        if old is None:
            raise NotFoundError("待交接session不存在")
        conversation = await session.get(ConversationRow, old.conversation_id)
        if (old.session_state != SessionState.WAITING_HANDOFF.value or conversation is None
                or conversation.state != ConversationState.ACTIVE.value
                or conversation.current_session_id != old.session_id):
            return None
        last_seq = await session.scalar(select(func.max(TurnRow.turn_seq)).where(
            TurnRow.session_id == old.session_id)) or 0
        unfinished = await session.scalar(select(TurnRow.turn_id).where(
            TurnRow.session_id == old.session_id, TurnRow.turn_state != TurnState.COMPLETE.value,
        ).limit(1))
        if unfinished is not None:
            return None
        job_row = await session.scalar(select(ContextJobRow).where(
            ContextJobRow.dedupe_key == f"prepare_handoff:{session_id}",
        ))
        if job_row is None:
            return None
        job = context_mappers.context_job_to_domain(job_row)
        frozen = job.frozen_through_turn_seq
        if frozen <= 0 or frozen >= last_seq:
            # 冻结点之后没有可带的原文（用户没有继续聊），兜底也交不出东西。
            return None
        tail = TurnRange(old.session_id, frozen + 1, last_seq)
        _, tail_payload, _ = await self._read_manifest_turns(session, tail)
        tail_tokens = await session.scalar(select(func.sum(TurnRow.turn_token_count)).where(
            TurnRow.session_id == old.session_id, TurnRow.turn_seq.between(frozen + 1, last_seq),
        )) or 0
        # 与正常交接同样的容量约束：尾部必须放得进新窗口。
        if tail_tokens >= self._lifecycle_policy.session_target_tokens:
            return None
        encoded = json.dumps({"snapshot": None, "tail": tail_payload},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        inherited = InheritedContextRef(old.session_id, None, tail,
            hashlib.sha256(encoded.encode("utf-8")).hexdigest())
        now = datetime.now(timezone.utc)
        old.session_state, old.session_end_time = SessionState.SEALED.value, now
        await session.flush()
        new_session = Session(
            session_id=IdGenerator.new("session"), conversation_id=old.conversation_id,
            state=SessionState.OPEN, progress_tokens=0, started_at=now,
            predecessor_session_id=old.session_id, inherited_context=inherited,
            session_seq=(await session.scalar(select(func.max(SessionRow.session_seq)).where(
                SessionRow.conversation_id == old.conversation_id))) + 1,
        )
        session.add(conversation_mappers.session_to_row(new_session, auto_successor_of=old.session_id))
        await session.flush()
        conversation.current_session_id, conversation.updated_at = new_session.session_id, now
        job = replace(job, status=ContextJobStatus.DONE, updated_at=now, finished_at=now)
        job_row.status, job_row.updated_at = job.status.value, now
        job_row.payload_json = context_mappers.context_job_payload_to_json(job)
        await session.flush()
        return new_session

    async def commit_handoff_in_transaction(
        self, session: AsyncSession, session_id: str,
    ) -> Session | None:
        """session_id是老session，同一事务读取READY材料和实际完整轮边界并交接，不自行提交或开启嵌套事务。"""
        successor = await session.scalar(select(SessionRow).where(
            SessionRow.auto_successor_of == session_id,
        ))
        if successor is not None:
            return conversation_mappers.session_to_domain(successor)
        old = await session.get(SessionRow, session_id)
        if old is None:
            raise NotFoundError("待交接session不存在")
        conversation = await session.get(ConversationRow, old.conversation_id)
        if (old.session_state != SessionState.WAITING_HANDOFF.value or conversation is None
                or conversation.state != ConversationState.ACTIVE.value
                or conversation.current_session_id != old.session_id):
            return None
        last_seq = await session.scalar(select(func.max(TurnRow.turn_seq)).where(TurnRow.session_id == old.session_id)) or 0
        unfinished = await session.scalar(select(TurnRow.turn_id).where(
            TurnRow.session_id == old.session_id, TurnRow.turn_state != TurnState.COMPLETE.value,
        ).limit(1))
        if unfinished is not None:
            return None
        snapshot_row = await session.scalar(select(ContextSnapshotRow).where(
            ContextSnapshotRow.session_id == session_id,
            ContextSnapshotRow.snapshot_state == SnapShotState.READY.value,
        ))
        if snapshot_row is None:
            return None
        snapshot_ref = context_mappers.snapshot_to_ref(snapshot_row)
        job_row = await session.get(ContextJobRow, snapshot_row.producer_job_id)
        if job_row is None or job_row.status != ContextJobStatus.READY.value:
            return None
        job = context_mappers.context_job_to_domain(job_row)
        if job.published_snapshot != snapshot_ref or job.plan is None:
            return None
        # 发布后可能发生原文修改；采用时再次核对固定来源。
        try:
            manifest = await self._read_source_manifest(session, job.plan)
        except (InvalidStateError, NotFoundError):
            return None
        if manifest != job.source_manifest:
            return None
        covered = snapshot_row.covered_through_turn_seq
        if covered > last_seq:
            return None
        tail = TurnRange(old.session_id, covered + 1, last_seq)
        tail_payload = []
        tail_tokens = 0
        if covered < last_seq:
            _, tail_payload, _ = await self._read_manifest_turns(session, tail)
            tail_tokens = await session.scalar(select(func.sum(TurnRow.turn_token_count)).where(
                TurnRow.session_id == old.session_id, TurnRow.turn_seq.between(covered + 1, last_seq),
            )) or 0
        # 不丢弃长尾部或创建一个已经没有原文空间的新窗口；不满足时继续等待。
        if snapshot_row.rendered_token_count + tail_tokens >= self._lifecycle_policy.session_target_tokens:
            return None
        encoded = json.dumps({"snapshot": asdict(snapshot_ref), "tail": tail_payload},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        inherited = InheritedContextRef(old.session_id, snapshot_ref, tail,
            hashlib.sha256(encoded.encode("utf-8")).hexdigest())
        now = datetime.now(timezone.utc)
        old.session_state, old.session_end_time = SessionState.SEALED.value, now
        await session.flush()
        new_session = Session(
            session_id=IdGenerator.new("session"), conversation_id=old.conversation_id,
            state=SessionState.OPEN, progress_tokens=0, started_at=now,
            predecessor_session_id=old.session_id, inherited_context=inherited,
            session_seq=(await session.scalar(select(func.max(SessionRow.session_seq)).where(
                SessionRow.conversation_id == old.conversation_id))) + 1,
        )
        session.add(conversation_mappers.session_to_row(new_session, auto_successor_of=old.session_id))
        await session.flush()
        conversation.current_session_id, conversation.updated_at = new_session.session_id, now
        snapshot_row.snapshot_state = SnapShotState.VALID.value
        job = replace(job, status=ContextJobStatus.DONE, updated_at=now, finished_at=now)
        job_row.status, job_row.updated_at = job.status.value, now
        job_row.payload_json = context_mappers.context_job_payload_to_json(job)
        await session.flush()
        return new_session

    async def fail_job(self, job_id: str, attempt_version: int, error: str, *,
                       expected_status: ContextJobStatus, expected_retry_count: int | None = None) -> bool:
        """把当前任务记录为失败；已经发布或结束的任务不能再改成失败。"""
        async with self._database.transaction() as session:
            row = await session.get(ContextJobRow, job_id)
            if row is None:
                raise NotFoundError("上下文任务不存在")
            job = context_mappers.context_job_to_domain(row)
            if job.attempt_version != attempt_version:
                return False
            if job.status == ContextJobStatus.FAILED:
                # 同一个失败已经记录过就返回True，保留第一次记录的错误和完成时间。
                return job.last_error == error
            # CAS检查：任务状态或重试次数已经变了，说明这次错误不该再处理，直接返回False。
            if (job.status != expected_status
                    or job.status not in (ContextJobStatus.NEED_GENERATE, ContextJobStatus.GENERATING,
                        ContextJobStatus.NEED_VALIDATE, ContextJobStatus.VALIDATING, ContextJobStatus.NEED_PUBLISH)
                    or (expected_retry_count is not None and job.retry_count != expected_retry_count)):
                return False
            now = datetime.now(timezone.utc)
            job = replace(job, status=ContextJobStatus.FAILED, last_error=error,
                updated_at=now, finished_at=now)
            row.payload_json = context_mappers.context_job_payload_to_json(job)
            row.status, row.updated_at = job.status.value, now
            await session.flush()
            return True

    async def retry_job(self, job_id: str, attempt_version: int, *, expected_status: ContextJobStatus,
                        available_at: datetime, error: str, restart_generation: bool = False,
                        expected_retry_count: int | None = None) -> bool:
        """临时失败后设置下次执行时间，保留已经保存的结果；来源变化时重新生成。"""
        async with self._database.transaction() as session:
            row = await session.get(ContextJobRow, job_id)
            if row is None:
                raise NotFoundError("上下文任务不存在")
            job = context_mappers.context_job_to_domain(row)
            # CAS检查：版本、状态和重试次数都要对得上，避免同一次错误被重复处理。
            if (job.attempt_version != attempt_version or job.status != expected_status
                    or job.status not in (ContextJobStatus.NEED_GENERATE, ContextJobStatus.GENERATING,
                                          ContextJobStatus.NEED_VALIDATE, ContextJobStatus.VALIDATING, ContextJobStatus.NEED_PUBLISH)
                    or (expected_retry_count is not None and job.retry_count != expected_retry_count)):
                return False
            # 生成没完成就回到待生成，校验没完成就回到待校验，已经保存的结果继续保留。
            running_stages = {
                ContextJobStatus.GENERATING: ContextJobStatus.NEED_GENERATE,
                ContextJobStatus.VALIDATING: ContextJobStatus.NEED_VALIDATE,
            }
            now = datetime.now(timezone.utc)
            changes = dict(status=ContextJobStatus.NEED_GENERATE if restart_generation else running_stages.get(job.status, job.status),
                # 中断的操作可能稍后才返回，把版本号加1，让它不能再保存结果。
                # 如果任务还没开始，只修改下次执行时间，就不需要增加版本号。
                attempt_version=job.attempt_version + int(restart_generation or job.status in running_stages),
                retry_count=job.retry_count + 1, last_error=error, available_at=available_at,
                updated_at=now, finished_at=None)
            # 原文或继承材料变了，旧计划、候选和校验结果就不能再用，清空后重新生成。
            if restart_generation:
                changes.update(plan=None, source_manifest=None, candidate=None, validation=None)
            job = replace(job, **changes)
            row.payload_json = context_mappers.context_job_payload_to_json(job)
            row.status, row.retry_count = job.status.value, job.retry_count
            row.available_at, row.updated_at = available_at, now
            await session.flush()
            return True

    async def recover_unfinished_attempts(self) -> int:
        """启动worker前调用：找出上次没做完的生成和校验，让它们可以重新执行。"""
        async with self._database.transaction() as session:
            rows = (await session.scalars(select(ContextJobRow).where(
                ContextJobRow.status.in_((ContextJobStatus.GENERATING.value, ContextJobStatus.VALIDATING.value)),
            ))).all()
            now = datetime.now(timezone.utc)
            for row in rows:
                job = context_mappers.context_job_to_domain(row)
                # 把版本号加1，拒绝上次没做完的操作之后才返回的结果；已经保存的候选继续保留。
                # 这里只修改任务状态，没有开始生成或处理新的错误，所以不增加attempt_count和retry_count。
                job = replace(job, status=(ContextJobStatus.NEED_GENERATE if job.status == ContextJobStatus.GENERATING
                                          else ContextJobStatus.NEED_VALIDATE),
                    attempt_version=job.attempt_version + 1,
                    last_error="应用启动恢复：上次上下文任务阶段中断",
                    available_at=now, updated_at=now, finished_at=None)
                row.payload_json = context_mappers.context_job_payload_to_json(job)
                row.status, row.available_at, row.updated_at = job.status.value, now, now
            await session.flush()
            return len(rows)
