"""共用任务表的调度与恢复；每条链限定自己的job_family。"""
from __future__ import annotations
from collections import defaultdict
from module.chat.conversation.core.domain.models import SessionState
from module.shared.common.errors.base import IdempotencyConflictError, InvalidStateError, NotFoundError
from module.shared.common.ids import IdGenerator
from module.shared.context.types.working import InheritedContext
from dataclasses import asdict, replace
from datetime import datetime, timezone
from module.memory_management.daily.core.domain.job import DailyJob
from module.memory_management.object.core.domain.job import ObjectJob
from module.memory_management.shared.contracts.errors import MemorySourceChangedError
from module.memory_management.shared.jobs.core.domain.command import CreateMemoryJob
from module.memory_management.shared.jobs.core.domain.job import MemoryJob, MemoryJobStatus, MemoryJobType
from module.memory_management.shared.contracts.source import SealedSessionSource
from module.memory_management.topic.core.domain.job import TopicJob
from sqlalchemy import and_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement
from module.context_compaction.repository import mappers as context_mappers
from module.memory_management.shared.jobs.repository import mappers as job_mappers
from storage.sqlite.value_codecs import SQLiteValueCodec
from module.chat.conversation.repository import mappers as conversation_mappers
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import (
    ContextSnapshotRow,
    ConversationRow,
    MemoryJobRow,
    MessageRow,
    SessionRow,
    TurnRow,
)


class MemoryJobRepository:
    """公共读写只处理所属链；无family实例用于管理查询。"""
    job_family: str | None = None
    job_class: type[MemoryJob]

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def _scope(self) -> ColumnElement[bool]:
        return true() if self.job_family is None else MemoryJobRow.payload_json["job_family"].as_string() == self.job_family

    def _owns(self, row: MemoryJobRow) -> bool:
        return self.job_family is None or row.payload_json["job_family"] == self.job_family

    async def create_job(self, command: CreateMemoryJob) -> str:
        """独立事务登记；批量调用可复用create_job_in_transaction。"""
        async with self._database.transaction() as session:
            return await self.create_job_in_transaction(session, command)

    async def create_job_in_transaction(self, session: AsyncSession, command: CreateMemoryJob) -> str:
        """共用幂等检查和初始任务写入，不提交调用方事务。"""
        self._validate_create_command(command)
        row = await session.scalar(select(MemoryJobRow).where(MemoryJobRow.dedupe_key == command.dedupe_key))
        if row is not None:
            existing = job_mappers.memory_job_to_domain(row)
            if (not isinstance(existing, self.job_class)
                    or (existing.job_type, existing.target_key, existing.priority, existing.input) != (
                        command.job_type, command.target_key, command.priority, command.input)):
                raise IdempotencyConflictError("dedupe_key已被其他记忆任务输入使用")
            return existing.job_id
        await session.flush()
        await self._validate_create_source(session, command)
        now = datetime.now(timezone.utc)
        job = self.job_class(
            job_id=IdGenerator.new("memory_job"), job_type=command.job_type,
            target_key=command.target_key, dedupe_key=command.dedupe_key, priority=command.priority,
            status=MemoryJobStatus.NEED_GENERATE, available_at=now, input=command.input,
            created_at=now, updated_at=now,
        )
        session.add(job_mappers.memory_job_to_row(job))
        await session.flush()
        return job.job_id

    def _validate_create_command(self, command: CreateMemoryJob) -> None:
        """子类检查本链命令及任务键。"""
        raise NotImplementedError("任务登记命令校验尚未实现")

    async def _validate_create_source(self, session: AsyncSession, command: CreateMemoryJob) -> None:
        """子类在登记事务内核对来源。"""
        raise NotImplementedError("任务登记来源校验尚未实现")

    def _unregistered_session_condition(self) -> ColumnElement[bool]:
        """各链定义自己的登记完成规则。"""
        raise NotImplementedError("封存session登记条件尚未实现")

    async def get_unregister_sealed_sessions(self, limit: int = 32) -> list[str]:
        """共用封存扫描、稳定排序和批次限制，只返回session ID。"""
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError("扫描数量必须在1到256之间")
        statement = select(SessionRow.session_id).join(
            ConversationRow, ConversationRow.conversation_id == SessionRow.conversation_id,
        ).where(
            SessionRow.session_state == SessionState.SEALED.value,
            self._unregistered_session_condition(),
        ).order_by(SessionRow.session_end_time, SessionRow.session_id).limit(limit)
        async with self._database.db_session() as session:
            return list((await session.scalars(statement)).all())

    async def read_session_character_id(self, session_id: str) -> str:
        """登记前读取角色归属，不加载对话正文。"""
        async with self._database.db_session() as session:
            return await self._read_session_character_id(session, session_id)

    async def _read_session_character_id(self, session: AsyncSession, session_id: str) -> str:
        sealed = await session.get(SessionRow, session_id)
        if sealed is None:
            raise NotFoundError("记忆来源session不存在")
        if sealed.session_state != SessionState.SEALED.value:
            raise InvalidStateError("仅封存后的session可用于记忆生成")
        conversation = await session.get(ConversationRow, sealed.conversation_id)
        if conversation is None:
            raise NotFoundError("记忆来源conversation不存在")
        return conversation.character_id

    async def read_sealed_session_source(self, session_id: str) -> SealedSessionSource:
        """同一短读事务加载角色归属和全部完整轮，保留跨日轮的原始时间。"""
        async with self._database.db_session() as session:
            return await self._read_sealed_session_source(session, session_id)

    async def _read_sealed_session_source(self, session: AsyncSession, session_id: str) -> SealedSessionSource:
        character_id = await self._read_session_character_id(session, session_id)
        turns = (await session.scalars(select(TurnRow).where(
            TurnRow.session_id == session_id,
        ).order_by(TurnRow.turn_seq))).all()
        messages = (await session.scalars(select(MessageRow).join(
            TurnRow, TurnRow.turn_id == MessageRow.turn_id,
        ).where(
            TurnRow.session_id == session_id,
            or_(MessageRow.role == "user", MessageRow.message_id == TurnRow.accepted_message_id),
        ).order_by(TurnRow.turn_seq, MessageRow.message_seq))).all()
        grouped = defaultdict(list)
        for message in messages:
            grouped[message.turn_id].append(message)
        # 复用完整轮映射；未完成轮或缺少正式回复明确报错，不静默跳过。
        return SealedSessionSource(
            session_id=session_id, character_id=character_id,
            turns=tuple(conversation_mappers.completed_turn_to_result(turn, grouped[turn.turn_id]) for turn in turns),
        )


    async def _read_inherited_source(self, session: AsyncSession, old_session: SessionRow,
                                     character_id: str) -> tuple[InheritedContext | None, dict | None]:
        """daily与object共用固定继承材料；仅加载前驱摘要和固定尾部原文。"""
        fixed_inherited = SQLiteValueCodec.inherited_context_from_json(old_session.inherited_context_json)
        if old_session.predecessor_session_id is not None and (fixed_inherited is None
                or fixed_inherited.predecessor_session_id != old_session.predecessor_session_id):
            raise InvalidStateError("记忆来源session缺少正确的固定继承材料")
        inherited_payload = None
        inherited_context = None
        if fixed_inherited is not None:
            snapshot_context = None
            tail = ()
            predecessor_row = await session.get(SessionRow, fixed_inherited.predecessor_session_id)
            if predecessor_row is None or predecessor_row.conversation_id != old_session.conversation_id:
                raise InvalidStateError("记忆继承前驱不存在或不属于当前对话")
            predecessor = await self._read_sealed_session_source(session, fixed_inherited.predecessor_session_id)
            if predecessor.character_id != character_id:
                raise InvalidStateError("记忆继承材料不属于当前角色")
            inherited_payload = {"snapshot": None, "tail": []}
            snapshot_ref = fixed_inherited.summary_snapshot_ref
            if snapshot_ref is not None:
                snapshot = await session.get(ContextSnapshotRow, snapshot_ref.snapshot_id)
                if snapshot is None:
                    raise NotFoundError("记忆固定继承摘要不存在")
                if (snapshot.session_id != predecessor.session_id or snapshot.snapshot_state != "valid"
                        or context_mappers.snapshot_to_ref(snapshot) != snapshot_ref):
                    raise MemorySourceChangedError("记忆固定继承摘要已失效或引用不一致")
                inherited_payload["snapshot"] = {"summary": snapshot.summary_json,
                    "rendered_text": snapshot.rendered_summary_text, "versions": snapshot.versions_json}
                snapshot_context = context_mappers.snapshot_to_context(snapshot)
            if fixed_inherited.source_range is not None:
                covered = snapshot_ref.covered_through_turn_seq if snapshot_ref is not None else 0
                if (fixed_inherited.source_range.from_seq != covered + 1
                        or fixed_inherited.source_range.through_seq < covered):
                    raise InvalidStateError("记忆继承原文未接续固定摘要覆盖点")
                tail = predecessor.select_turns(fixed_inherited.source_range)
                inherited_payload["tail"] = [asdict(turn) for turn in tail]
            inherited_context = InheritedContext(fixed_inherited, snapshot_context, tail)
        return inherited_context, inherited_payload


    async def _begin_generate(self, job_id: str, job_type: MemoryJobType) -> MemoryJob | None:
        """核对到期队首，登记生成代次并进入GENERATING，不加载来源。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("记忆任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, self.job_class):
                raise InvalidStateError("任务不属于当前记忆链")
            now = datetime.now(timezone.utc)
            if job.status != MemoryJobStatus.NEED_GENERATE or job.available_at > now:
                return None
            if job.job_type != job_type:
                raise InvalidStateError("生成任务类型与当前入口不一致")
            # 找同目标的更早任务
            earlier = await session.scalar(select(MemoryJobRow.job_id).where(
                self._scope(),
                MemoryJobRow.target_key == job.target_key,
                MemoryJobRow.status.not_in((MemoryJobStatus.DONE.value, MemoryJobStatus.FAILED.value,
                                            MemoryJobStatus.OBSOLETE.value)),
                or_(MemoryJobRow.created_at < job.created_at,
                    and_(MemoryJobRow.created_at == job.created_at, MemoryJobRow.job_id < job.job_id)),
            ).limit(1))
            if earlier is not None:
                return None
            # 更新任务状态
            changes = {}
            if isinstance(job, DailyJob):
                changes.update(plan=None, candidate=None, review=None, write_result=None)
            elif isinstance(job, TopicJob):
                changes.update(plan=None, generation_result=None, review=None)
            elif isinstance(job, ObjectJob):
                changes.update(objects=(), selection_notes=())
            job = replace(
                job,
                source_manifest=None,
                attempt_version=job.attempt_version + 1,
                attempt_count=job.attempt_count + 1,
                status=MemoryJobStatus.GENERATING,
                vector_document=None,
                storage_job_id=None, result=None, reviewed_version=None,
                reviewed_hash=None, applied_revision=None,
                last_error=None, error_code=None, finished_at=None, updated_at=now, **changes,
            )
            # 更新数据库任务状态
            row.status = job.status.value
            row.updated_at = now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return job

    async def get_job(self, job_id: str) -> MemoryJob | None:
        """按ID读取完整业务任务；不存在返回None，不推进状态。"""
        async with self._database.db_session() as session:
            row = await session.get(MemoryJobRow, job_id)
            return job_mappers.memory_job_to_domain(row) if row is not None and self._owns(row) else None

    async def get_next_ready_job(self) -> MemoryJob | None:
        """
        ① 扫描待执行阶段及遗留执行态；后者交给唯一workflow恢复，不重复调用模型。
        waiting_index 不在可领列表，它由storage job推进。
        ② 到期检查：retry_job保留最近已提交成果，退回对应待执行阶段并设置available_at。
        ③ 同目标队首规则：同family、同 target_key 下存在更早（created_at, job_id 全序）的非终态任务不能被选。
        """
        executable_statuses = (
            MemoryJobStatus.NEED_GENERATE.value,
            MemoryJobStatus.GENERATING.value,
            MemoryJobStatus.NEED_REVIEW.value,
            MemoryJobStatus.REVIEWING.value,
            MemoryJobStatus.NEED_WRITE.value,
            MemoryJobStatus.WRITING.value,
            MemoryJobStatus.NEED_INDEX.value,
            MemoryJobStatus.INDEXING.value,
            MemoryJobStatus.NEED_ACTIVATE.value,
        )
        blocking_statuses = (*executable_statuses, MemoryJobStatus.WAITING_INDEX.value)
        earlier = aliased(MemoryJobRow)
        # 更早任务的available_at不参与判断，退避时仍保留同目标队首位置。
        has_earlier = select(earlier.job_id).where(
            earlier.target_key == MemoryJobRow.target_key,
            earlier.payload_json["job_family"].as_string() == MemoryJobRow.payload_json["job_family"].as_string(),
            earlier.status.in_(blocking_statuses),
            or_(
                earlier.created_at < MemoryJobRow.created_at,
                and_(earlier.created_at == MemoryJobRow.created_at, earlier.job_id < MemoryJobRow.job_id),
            ),
        ).correlate(MemoryJobRow).exists()
        statement = select(MemoryJobRow).where(
            self._scope(),
            MemoryJobRow.status.in_(executable_statuses),
            MemoryJobRow.available_at <= datetime.now(timezone.utc),
            ~has_earlier,
        ).order_by(MemoryJobRow.priority, MemoryJobRow.created_at, MemoryJobRow.job_id).limit(1)
        async with self._database.db_session() as session:
            row = await session.scalar(statement)
            return job_mappers.memory_job_to_domain(row) if row is not None and self._owns(row) else None

    async def retry_job(self, job_id: str, attempt_version: int, *, expected_status: MemoryJobStatus,
                        available_at: datetime, error: str, restart_generation: bool = False,
                        expected_retry_count: int | None = None) -> bool:
        """运行期单个任务失败后的登记续跑：某次具体执行失败了，按错误类型决定是退避重试当前阶段、还是整链重生成、还是放弃。"""
        running_stages = {
            MemoryJobStatus.GENERATING: MemoryJobStatus.NEED_GENERATE,
            MemoryJobStatus.REVIEWING: MemoryJobStatus.NEED_REVIEW,
            MemoryJobStatus.WRITING: MemoryJobStatus.NEED_WRITE,
            MemoryJobStatus.INDEXING: MemoryJobStatus.NEED_INDEX,
        }
        active = (*running_stages, *running_stages.values(),
                  MemoryJobStatus.NEED_INDEX, MemoryJobStatus.NEED_ACTIVATE)
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None or not self._owns(row):
                raise NotFoundError("记忆任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if (job.attempt_version != attempt_version or job.status != expected_status or job.status not in active
                    or (expected_retry_count is not None and job.retry_count != expected_retry_count)):
                return False
            published = job.publication_started
            if published and (restart_generation or job.status != MemoryJobStatus.NEED_ACTIVATE):
                return False
            if job.available_at == available_at and job.last_error == error:
                return False
            now = datetime.now(timezone.utc)
            invalidate = restart_generation or job.status in running_stages
            changes = dict(
                status=MemoryJobStatus.NEED_GENERATE if restart_generation else running_stages.get(job.status, job.status),
                attempt_version=job.attempt_version + int(invalidate), retry_count=job.retry_count + 1,
                available_at=available_at, last_error=error, error_code="memory_source_changed" if restart_generation else "memory_stage_failed",
                updated_at=now, finished_at=None,
            )
            if restart_generation:
                changes.update(source_manifest=None, vector_document=None,
                    storage_job_id=None, result=None, applied_revision=None,
                    reviewed_version=None, reviewed_hash=None)
                if isinstance(job, (DailyJob, TopicJob)):
                    changes.update(plan=None, review=None)
                    changes["candidate" if isinstance(job, DailyJob) else "generation_result"] = None
                if isinstance(job, DailyJob):
                    changes["plan"] = None
                    changes["write_result"] = None
                elif isinstance(job, TopicJob):
                    changes["generation_source"] = None
                    changes["write_results"] = None
                elif isinstance(job, ObjectJob):
                    changes["objects"] = ()
                    changes["selection_notes"] = ()
            job = replace(job, **changes)
            row.status, row.retry_count, row.available_at = job.status.value, job.retry_count, job.available_at
            row.updated_at, row.payload_json = now, job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True

    async def fail_job(self, job_id: str, attempt_version: int, error: str, *, expected_status: MemoryJobStatus,
                       expected_retry_count: int | None = None) -> bool:
        """只结束当前阶段和代次；保留排查材料及已经生效的正式版本。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None or not self._owns(row):
                raise NotFoundError("记忆任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if job.attempt_version != attempt_version:
                return False
            if job.status == MemoryJobStatus.FAILED:
                return job.last_error == error
            if (job.status != expected_status or job.status in (MemoryJobStatus.DONE, MemoryJobStatus.OBSOLETE,
                    MemoryJobStatus.WAITING_INDEX)
                    or (expected_retry_count is not None and job.retry_count != expected_retry_count)):
                return False
            now = datetime.now(timezone.utc)
            job = replace(job, status=MemoryJobStatus.FAILED, last_error=error,
                error_code="memory_job_failed", updated_at=now, finished_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True

    async def recover_unfinished_attempts(self) -> int:
        """仅恢复当前链遗留执行态；调用前必须确保该链旧协程已结束。"""
        running_stages = {
            MemoryJobStatus.GENERATING: MemoryJobStatus.NEED_GENERATE,
            MemoryJobStatus.REVIEWING: MemoryJobStatus.NEED_REVIEW,
            MemoryJobStatus.WRITING: MemoryJobStatus.NEED_WRITE,
            MemoryJobStatus.INDEXING: MemoryJobStatus.NEED_INDEX,
        }
        async with self._database.transaction() as session:
            rows = (await session.scalars(select(MemoryJobRow).where(
                self._scope(),
                MemoryJobRow.status.in_(tuple(status.value for status in running_stages)),
            ))).all()
            now = datetime.now(timezone.utc)
            for row in rows:
                job = job_mappers.memory_job_to_domain(row)
                job = replace(job, status=running_stages[job.status],
                    attempt_version=job.attempt_version + 1,
                    available_at=now, updated_at=now, finished_at=None,
                    last_error="应用启动恢复：上次记忆任务阶段中断")
                row.status, row.available_at, row.updated_at = job.status.value, now, now
                row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return len(rows)
