"""SQLite daily任务登记、阶段产物和正式发布。"""
from __future__ import annotations
import hashlib
import json
from module.chat.conversation.core.domain.models import SessionState, TurnState
from module.shared.common.errors.base import InvalidStateError, NotFoundError
from module.memory_management.shared.core.domain.document import VectorDocument
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from module.memory_management.daily.core.domain.generation import DailyCandidate
from module.memory_management.daily.core.domain.plan import DailyPlan
from module.memory_management.daily.core.domain.job import DailyJob, DailyJobInput
from module.memory_management.daily.core.domain.publication import DailyWrite
from module.memory_management.daily.core.domain.review import DailyReview
from module.memory_management.daily.core.domain.source import DailySourceInput
from module.memory_management.shared.contracts.errors import MemorySourceChangedError
from module.memory_management.shared.jobs.core.domain.command import CreateMemoryJob
from module.memory_management.shared.jobs.core.domain.job import MemoryJobStatus, MemoryJobType
from module.memory_management.shared.contracts.source import MemorySourceManifest
from module.memory_management.shared.contracts.review import ReviewVerdict
from module.memory_management.shared.jobs.repository.sqlite import MemoryJobRepository
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from module.memory_management.daily.repository import mappers as daily_mappers
from module.memory_management.shared.repository import document_mappers
from module.memory_management.shared.jobs.repository import mappers as job_mappers
from storage.sqlite.value_codecs import SQLiteValueCodec
from storage.sqlite.models import DailyMemoryRow, MemoryJobRow, MessageRow, SessionRow, TurnRow
from typing import cast
from zoneinfo import ZoneInfo
from module.shared.common.time import BackendTime


LEGACY_DAILY_ZONE = ZoneInfo("Asia/Shanghai")


class DailyProductionRepository(MemoryJobRepository):
    """登记任务并保存阶段产物，不在事务内调用模型。"""
    job_family = "daily"
    job_class = DailyJob

    def _validate_create_command(self, command: CreateMemoryJob) -> None:
        """检查daily类型、日期、来源身份及任务键。"""
        if command.job_type != MemoryJobType.CREATE_DAILY:
            raise NotImplementedError("当前仅实现daily任务登记")
        source = command.input
        if not isinstance(source, DailyJobInput):
            raise InvalidStateError("daily任务必须使用DailyJobInput")
        if (not source.character_id or type(source.memory_date) is not date
                or len(source.source_session_ids) != 1 or not source.source_session_ids[0]
                or source.target_kind not in (None, MemoryKind.DAILY)
                or type(command.priority) is not int):
            raise InvalidStateError("daily登记需要角色、日期、单个来源session和整数优先级")
        session_id = source.source_session_ids[0]
        expected = DailyJobInput.create_command(
            source.character_id,
            session_id,
            source.memory_date,
            source.source_message_ids,
            priority=command.priority,
        )
        if (command.target_key != expected.target_key or command.dedupe_key != expected.dedupe_key):
            raise InvalidStateError("daily任务键与角色、来源session或日期不一致")

    async def _validate_create_source(self, session: AsyncSession, command: CreateMemoryJob) -> None:
        """daily按日期拥有完整轮，登记时核对本日期的完整来源范围。"""
        source = command.input
        session_id = source.source_session_ids[0]
        sealed_source = await self._read_sealed_session_source(session, session_id)
        if sealed_source.character_id != source.character_id:
            raise InvalidStateError("daily来源session不属于指定角色")
        daily_ids = set(sealed_source.group_daily_message_ids().get(source.memory_date, ()))
        if not daily_ids or (source.source_message_ids and set(source.source_message_ids) != daily_ids):
            raise InvalidStateError("daily来源必须完整覆盖该日期归属的全部完整轮")
        if len(source.source_message_ids) != len(set(source.source_message_ids)):
            raise InvalidStateError("daily来源消息ID重复")

    def _unregistered_session_condition(self) -> ColumnElement[bool]:
        # 查找没有登记daily任务的session
        return SessionRow.daily_jobs_registered_at.is_(None)

    async def register_daily_jobs_for_session(self, session_id: str,
                                              commands: tuple[CreateMemoryJob, ...]) -> tuple[str, ...]:
        """独立后台事务：给session登记任务（多天session就多个任务），确认完整来源覆盖→逐日期幂等登记→写完成标记。"""
        async with self._database.transaction() as session:
            old_session = await session.get(SessionRow, session_id)
            if old_session is None:
                raise NotFoundError("daily来源session不存在")
            if old_session.session_state != SessionState.SEALED.value:
                raise InvalidStateError("仅封存后的session可登记daily任务")
            if old_session.daily_jobs_registered_at is not None:
                return ()
            # 校验业务层批次覆盖本session全部消息，防止漏掉一个日期后提前打标。
            if any(command.job_type != MemoryJobType.CREATE_DAILY
                   or command.input.source_session_ids != (session_id,) for command in commands):
                raise InvalidStateError("daily批次包含其他任务类型或来源session")
            dates = [command.input.memory_date for command in commands]
            if len(dates) != len(set(dates)):
                raise InvalidStateError("daily批次重复登记同一日期")
            unfinished = await session.scalar(select(TurnRow.turn_id).where(
                TurnRow.session_id == session_id, TurnRow.turn_state != TurnState.COMPLETE.value,
            ).limit(1))
            if unfinished is not None:
                raise InvalidStateError("daily来源仍有未完成轮")
            actual_ids = set((await session.scalars(select(MessageRow.message_id).join(
                TurnRow, MessageRow.turn_id == TurnRow.turn_id,
            ).where(
                TurnRow.session_id == session_id,
                or_(MessageRow.role == "user", MessageRow.message_id == TurnRow.accepted_message_id),
            ))).all())
            requested_ids = [message_id for command in commands for message_id in command.input.source_message_ids]
            if set(requested_ids) != actual_ids or len(requested_ids) != len(actual_ids):
                raise InvalidStateError("daily批次未完整且唯一地覆盖来源消息")
            job_ids = []
            for command in commands:
                job_ids.append(await self.create_job_in_transaction(session, command))
            old_session.daily_jobs_registered_at = datetime.now(timezone.utc)
            await session.flush()
            return tuple(job_ids)

    async def begin_generate(self, job_id: str) -> DailyJob | None:
        """共用生成入口，只登记代次与执行态，不加载来源。"""
        return cast(DailyJob | None, await self._begin_generate(job_id, MemoryJobType.CREATE_DAILY))

    async def begin_review(self, job_id: str) -> DailyJob | None:
        """读取候选并登记审核执行态，不重新读取或规划来源。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("记忆任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, DailyJob) or job.job_type != MemoryJobType.CREATE_DAILY:
                raise InvalidStateError("该入口只处理daily生成候选审核")
            now = datetime.now(timezone.utc)
            if job.status != MemoryJobStatus.NEED_REVIEW or job.available_at > now:
                return None
            if job.candidate is None or job.plan is None or job.source_manifest is None:
                raise InvalidStateError("daily审核缺少已保存的候选或固定来源")
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
            job = replace(job, status=MemoryJobStatus.REVIEWING,
                attempt_version=job.attempt_version + 1, attempt_count=job.attempt_count + 1,
                last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return job

    async def _load_daily_source(self, session: AsyncSession, job: DailyJob,
                                 plan: DailyPlan | None = None) -> DailySourceInput:
        """一次读取完整来源；无候选时现场规划，有候选时核对其固定计划。"""
        if job.input.memory_date is None or len(job.input.source_session_ids) != 1:
            raise InvalidStateError("daily来源需要归属日期和单个session")
        session_id = job.input.source_session_ids[0]
        source = await self._read_sealed_session_source(session, session_id)
        if source.character_id != job.input.character_id:
            raise InvalidStateError("daily来源session不属于任务登记的角色")
        current = await session.scalar(select(DailyMemoryRow).where(
            DailyMemoryRow.character_id == source.character_id,
            DailyMemoryRow.memory_date == job.input.memory_date,
        ))
        base_ref = MemoryRevisionRef(MemoryKind.DAILY, current.daily_id, current.revision,
                                    current.content_hash) if current is not None else None
        old_session = await session.get(SessionRow, session_id)
        fixed_inherited = SQLiteValueCodec.inherited_context_from_json(old_session.inherited_context_json)
        expected_ids = job.input.source_message_ids
        if not expected_ids and job.source_manifest is not None:
            expected_ids = job.source_manifest.source_message_ids
        if not expected_ids:
            expected_ids = source.group_daily_message_ids().get(job.input.memory_date, ())
            if not expected_ids:
                expected_ids = source.group_daily_message_ids(zone=LEGACY_DAILY_ZONE).get(job.input.memory_date, ())
        if plan is None:
            plan = DailyPlan.plan_daily(source, memory_date=job.input.memory_date,
                source_message_ids=expected_ids,
                inherited_context=fixed_inherited, base_daily_ref=base_ref)
        if (plan.session_id != session_id or plan.character_id != source.character_id
                or plan.memory_date != job.input.memory_date):
            raise InvalidStateError("daily计划与任务登记的session、角色或日期不一致")
        if plan.base_daily_ref != base_ref:
            raise MemorySourceChangedError("daily计划的基础版本已变化，需要重新生成")
        if job.input.target_id is not None and (base_ref is None or base_ref.memory_id != job.input.target_id):
            raise InvalidStateError("daily计划的基础文档与登记目标不一致")
        owned = source.select_turns(plan.source_turns)
        owned_ids = tuple(message.message_id for turn in owned
                          for message in (*turn.user_messages, turn.accepted_reply))
        if len(owned_ids) != len(expected_ids) or set(owned_ids) != set(expected_ids):
            raise InvalidStateError("daily计划未完整覆盖归属日期的原始完整轮")
        context = source.select_turns(plan.context_only_turns) if plan.context_only_turns is not None else ()
        if plan.inherited_context != fixed_inherited:
            raise MemorySourceChangedError("daily计划没有沿用session固定继承引用")
        inherited_context, inherited_payload = await self._read_inherited_source(
            session, old_session, source.character_id)
        scopes = [plan.source_turns]
        if plan.context_only_turns is not None:
            scopes.append(plan.context_only_turns)
        if fixed_inherited is not None and fixed_inherited.source_range is not None:
            scopes.append(fixed_inherited.source_range)
        revisions = (await session.execute(select(TurnRow.session_id, TurnRow.turn_seq,
            TurnRow.turn_id, TurnRow.content_revision).where(or_(*(
                and_(TurnRow.session_id == scope.session_id,
                     TurnRow.turn_seq.between(scope.from_seq, scope.through_seq)) for scope in scopes
            ))).order_by(TurnRow.session_id, TurnRow.turn_seq))).all()
        payload = {
            "plan": daily_mappers.daily_plan_to_json(plan),
            "owned_turns": [asdict(turn) for turn in owned],
            "context_only_turns": [asdict(turn) for turn in context],
            "base_daily": {"ref": SQLiteValueCodec.memory_ref_to_json(base_ref),
                           "document": current.document_json,
                           "vector_document_key": current.vector_document_key} if current is not None else None,
            "inherited_context": inherited_payload,
            "turn_revisions": [list(row) for row in revisions],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                             default=SQLiteValueCodec.datetime_to_json)
        manifest = MemorySourceManifest(
            character_id=plan.character_id, source_session_ids=(plan.session_id,),
            source_message_ids=owned_ids, source_content_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            memory_date=plan.memory_date, base_ref=base_ref,
        )
        return DailySourceInput(
            plan=plan, source_manifest=manifest, owned_turns=owned, context_only_turns=context,
            inherited_context=inherited_context,
            base_daily=document_mappers.daily_memory_to_domain(current).document if current is not None else None,
        )

    async def read_daily_source(self, job: DailyJob) -> DailySourceInput:
        """生成时一次准备全部材料；审核时核对与候选一起保存的来源。"""
        if (job.plan is None) != (job.source_manifest is None):
            raise InvalidStateError("daily已保存的计划与来源清单不完整")
        if job.candidate is not None and job.plan is None:
            raise InvalidStateError("daily已保存候选缺少固定计划和来源")
        async with self._database.db_session() as session:
            source = await self._load_daily_source(session, job, job.plan)
        if job.source_manifest is not None and source.source_manifest != job.source_manifest:
            raise MemorySourceChangedError("daily固定来源已变化，需要重新规划")
        return source


    async def save_candidate(self, job_id: str, attempt_version: int,
                             candidate: DailyCandidate, *,
                             source: DailySourceInput | None = None) -> bool:
        """提交生成恢复点：计划、固定来源、模型候选和下一状态同事务保存。
            需要做CAS检查，确保与生成任务时的初始状态一致
        """
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("记忆任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, DailyJob):
                raise NotImplementedError("当前仅实现daily候选保存")
            if source is None or not isinstance(candidate, DailyCandidate):
                raise InvalidStateError("daily候选保存需要本次生成材料和DailyCandidate")
            # CAS检查：版本对不上，说明别人已重新登记了更新的尝试，这次提交过期，直接拒绝。
            if job.attempt_version != attempt_version:
                return False
            # CAS检查：如果已经往后推进了，return True，反之出现问题，return False
            if job.status != MemoryJobStatus.GENERATING:
                return (job.status in (MemoryJobStatus.NEED_REVIEW, MemoryJobStatus.REVIEWING,
                    MemoryJobStatus.NEED_WRITE, MemoryJobStatus.WRITING, MemoryJobStatus.NEED_INDEX, MemoryJobStatus.INDEXING, MemoryJobStatus.WAITING_INDEX,
                    MemoryJobStatus.NEED_ACTIVATE, MemoryJobStatus.DONE)
                    and job.candidate == candidate and job.plan == source.plan
                    and job.source_manifest == source.source_manifest)
            plan = source.plan
            # 装填检查：内容是否有漂移
            if (plan.character_id != job.input.character_id or plan.memory_date != job.input.memory_date
                    or (plan.session_id,) != job.input.source_session_ids
                    or candidate.document.memory_date != plan.memory_date
                    or plan.session_id not in candidate.document.source_session_ids):
                raise InvalidStateError("daily生成结果与任务登记不一致")
            # CAS检查：仅核对基础版本，不重复读取整份聊天；原文漂移由审核时的固定来源校验发现。
            current = await session.scalar(select(DailyMemoryRow).where(
                DailyMemoryRow.character_id == plan.character_id, DailyMemoryRow.memory_date == plan.memory_date,
            ))
            base_ref = MemoryRevisionRef(MemoryKind.DAILY, current.daily_id, current.revision,
                current.content_hash) if current is not None else None
            if plan.base_daily_ref != base_ref or (current is not None
                    and (source.base_daily is None
                         or current.document_json != document_mappers._memory_daily_document_to_json(source.base_daily))):
                raise MemorySourceChangedError("daily基础版本已变化，需要重新生成")
            now = datetime.now(timezone.utc)
            # 更新任务状态，generating -> need_review
            job = replace(job, plan=plan, source_manifest=source.source_manifest, candidate=candidate,
                status=MemoryJobStatus.NEED_REVIEW, last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True


    async def save_review(self, job_id: str, attempt_version: int, review: DailyReview, *, retry_rejected: bool = True) -> bool:
        """提交审核恢复点：计划、固定来源、模型候选和下一状态同事务保存。
            需要做CAS检查，确保与生成任务时的初始状态一致
        """
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("记忆任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, DailyJob) or not isinstance(review, DailyReview):
                raise NotImplementedError("当前仅实现daily审核保存")
            if job.attempt_version != attempt_version:
                return False
            # 状态检查：如果已经往后推进了，return True，反之出现问题，return False
            if job.status != MemoryJobStatus.REVIEWING:
                return job.review == review and job.status in (
                    MemoryJobStatus.NEED_WRITE, MemoryJobStatus.WRITING, MemoryJobStatus.NEED_INDEX, MemoryJobStatus.INDEXING, MemoryJobStatus.WAITING_INDEX,
                    MemoryJobStatus.NEED_ACTIVATE, MemoryJobStatus.DONE,
                    MemoryJobStatus.NEED_GENERATE, MemoryJobStatus.FAILED)
            if job.candidate is None:
                raise InvalidStateError("审核任务缺少候选")
            review.validate(job.candidate.document)
            now = datetime.now(timezone.utc)
            rejected = review.review_result == ReviewVerdict.REJECT
            job = replace(job, review=review,
                reviewed_version=job.candidate.candidate_version,
                reviewed_hash=job.candidate.content_hash,
                status=((MemoryJobStatus.NEED_GENERATE if retry_rejected else MemoryJobStatus.FAILED)
                        if rejected else MemoryJobStatus.NEED_WRITE),
                reextraction_count=job.reextraction_count + int(rejected),
                finished_at=now if rejected and not retry_rejected else None,
                last_error=review.feedback() if rejected else None,
                error_code="daily_review_rejected" if rejected else None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True

    async def begin_write(self, job_id: str) -> DailyJob | None:
        """读取已审核正文，登记写入代次；事务外进行渲染。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("记忆任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, DailyJob) or job.job_type != MemoryJobType.CREATE_DAILY:
                raise InvalidStateError("该入口只处理daily发布准备")
            now = datetime.now(timezone.utc)
            if job.status != MemoryJobStatus.NEED_WRITE or job.available_at > now:
                return None
            # 状态检查：确保没有更早的待发布任务
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
            job.final_document()
            job = replace(job, status=MemoryJobStatus.WRITING,
                attempt_version=job.attempt_version + 1, attempt_count=job.attempt_count + 1,
                last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return job

    async def save_write(self, job_id: str, attempt_version: int, result: DailyWrite) -> bool:
        """保存任务侧结构化待发布正文，正式daily等待索引确认后激活。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("记忆任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, DailyJob) or not isinstance(result, DailyWrite):
                raise InvalidStateError("daily发布准备需要DailyWrite")
            if job.attempt_version != attempt_version:
                return False
            # 状态检查：确保没有更早的待发布任务
            if job.status != MemoryJobStatus.WRITING:
                return job.write_result == result and job.status in (
                    MemoryJobStatus.NEED_INDEX, MemoryJobStatus.INDEXING, MemoryJobStatus.WAITING_INDEX,
                    MemoryJobStatus.NEED_ACTIVATE, MemoryJobStatus.DONE)
            if result.document != job.final_document():
                raise InvalidStateError("待发布正文与审核结果不一致")
            now = datetime.now(timezone.utc)
            job = replace(job, write_result=result, status=MemoryJobStatus.NEED_INDEX,
                          last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True

    async def begin_index(self, job_id: str) -> DailyJob | None:
        """登记索引执行代次；向量化和Qdrant写入不在事务内。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("记忆任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, DailyJob) or job.job_type != MemoryJobType.CREATE_DAILY:
                raise InvalidStateError("该入口只处理daily发布准备")
            now = datetime.now(timezone.utc)
            if job.status != MemoryJobStatus.NEED_INDEX or job.available_at > now:
                return None
            # 状态检查：确保没有更早的待发布任务
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
            job.index_document()
            job = replace(job, status=MemoryJobStatus.INDEXING,
                attempt_version=job.attempt_version + 1, attempt_count=job.attempt_count + 1,
                last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return job

    async def save_index(self, job_id: str, attempt_version: int, document: VectorDocument) -> bool:
        """Qdrant确认成功后提交进度；不保存向量或覆盖正式daily。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("记忆任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, DailyJob):
                raise InvalidStateError("当前只支持daily索引完成")
            if job.attempt_version != attempt_version:
                return False
            # 状态检查：确保没有更早的待发布任务
            if job.status != MemoryJobStatus.INDEXING:
                return job.vector_document == document and job.status in (
                    MemoryJobStatus.NEED_ACTIVATE, MemoryJobStatus.DONE)
            if document != job.index_document():
                raise InvalidStateError("索引完成引用与固定正文不一致")
            now = datetime.now(timezone.utc)
            job = replace(job, vector_document=document, status=MemoryJobStatus.NEED_ACTIVATE,
                          last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True


    async def activate_revision(self, job_id: str, attempt_version: int) -> bool:
        """在同一SQLite事务中激活正式JSON、保存结果并完成任务。
        """
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("记忆任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, DailyJob):
                raise NotImplementedError("当前只实现daily发布")
            if job.attempt_version != attempt_version:
                return False
            if job.status == MemoryJobStatus.DONE:
                return True
            if job.status != MemoryJobStatus.NEED_ACTIVATE or job.available_at > datetime.now(timezone.utc):
                return False
            if job.vector_document is None or job.vector_document != job.index_document():
                raise InvalidStateError("daily索引未确认或与待发布正文不一致")
            ref = job.vector_document.ref
            current = await session.scalar(select(DailyMemoryRow).where(
                DailyMemoryRow.character_id == job.input.character_id,
                DailyMemoryRow.memory_date == job.write_result.document.memory_date))
            current_ref = MemoryRevisionRef(MemoryKind.DAILY, current.daily_id, current.revision,
                current.content_hash) if current is not None else None
            if job.result is not None:
                # job.result 非空说明旧任务已持久化正式版本；兼容迁移前留下的发布恢复点。此时只允许“认领”同一个版本
                if job.result != ref or current_ref != ref:
                    raise MemorySourceChangedError("已发布daily发生变化，不能覆盖较新正文")
            else:
                # CAS——登记表当前版必须仍然等于整个流水线开工时计划的 base_daily_ref
                if current_ref != job.plan.base_daily_ref:
                    raise MemorySourceChangedError("正式daily基础版本已变化，需要重新生成")
                now = datetime.now(timezone.utc)
                # 改变ORM的值，sqlite会自动追踪变化
                values = dict(character_id=job.input.character_id,
                    memory_date=job.write_result.document.memory_date,
                    source_session_ids_json=list(job.write_result.document.source_session_ids),
                    document_json=job.write_result.document.to_data(), revision=ref.revision,
                    content_hash=ref.content_hash, vector_document_key=job.vector_document.vector_document_key,
                    published_at=now, recall_tier=job.input.recall_tier.value,
                    updated_at=now)
                if current is None:
                    # 第一版daily直接新增
                    session.add(DailyMemoryRow(daily_id=ref.memory_id, created_at=now,
                                               last_mentioned_at=BackendTime.local(now).date(), **values))
                else:
                    # 更新daily
                    if current.last_mentioned_at is None:
                        current.last_mentioned_at = BackendTime.local(now).date()
                    for key, value in values.items():
                        setattr(current, key, value)
                job = replace(job, result=ref, applied_revision=ref.revision, updated_at=now)
                row.updated_at, row.payload_json = now, job_mappers.memory_job_payload_to_json(job)
                await session.flush()
            now = datetime.now(timezone.utc)
            job = replace(job, status=MemoryJobStatus.DONE, last_error=None,
                          error_code=None, finished_at=now, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True
