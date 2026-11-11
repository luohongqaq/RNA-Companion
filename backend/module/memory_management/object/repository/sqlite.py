"""Object任务登记、候选保存、索引进度与正式激活。"""
from __future__ import annotations
from module.shared.common.errors.base import IdempotencyConflictError, InvalidStateError, NotFoundError
from dataclasses import replace
from datetime import datetime, timezone
from module.memory_management.object.core.domain.job import ObjectJob, ObjectJobInput
from module.memory_management.object.core.domain.document import MemoryObject
from module.memory_management.object.core.domain.generation import ObjectSelectionNote
from module.memory_management.object.core.domain.source import ObjectSourceInput
from module.memory_management.shared.contracts.errors import MemorySourceChangedError
from module.memory_management.shared.jobs.core.domain.command import CreateMemoryJob
from module.memory_management.shared.jobs.core.domain.job import MemoryJobStatus, MemoryJobType
from module.memory_management.shared.jobs.repository.sqlite import MemoryJobRepository
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from module.memory_management.shared.jobs.repository import mappers as job_mappers
from module.memory_management.object.repository import mappers as object_mappers
from storage.sqlite.models import ConversationRow, MemoryJobRow, MemoryObjectRow, SessionRow
from typing import cast


class ObjectProductionRepository(MemoryJobRepository):
    job_family = "object"
    job_class = ObjectJob

    def _validate_create_command(self, command: CreateMemoryJob) -> None:
        """检查object类型、来源身份及任务键。"""
        if command.job_type != MemoryJobType.EXTRACT_OBJECTS or not isinstance(command.input, ObjectJobInput):
            raise InvalidStateError("object登记需要EXTRACT_OBJECTS和ObjectJobInput")
        source = command.input
        if (not source.character_id or len(source.source_session_ids) != 1 or not source.source_session_ids[0]
                or type(command.priority) is not int or source.source_message_ids):
            raise InvalidStateError("object登记需要角色、单个完整session和整数优先级，不指定消息子集")
        session_id = source.source_session_ids[0]
        expected = ObjectJobInput.create_command(source.character_id, session_id, priority=command.priority)
        if command.target_key != expected.target_key or command.dedupe_key != expected.dedupe_key:
            raise InvalidStateError("object任务键与角色或来源session不一致")

    async def _validate_create_source(self, session: AsyncSession, command: CreateMemoryJob) -> None:
        """object拥有整个封存session；核对角色归属，不加载正文。"""
        source = command.input
        character_id = await self._read_session_character_id(session, source.source_session_ids[0])
        if character_id != source.character_id:
            raise InvalidStateError("object来源session不属于指定角色")

    def _unregistered_session_condition(self) -> ColumnElement[bool]:
        registered = select(MemoryJobRow.job_id).where(
            MemoryJobRow.job_type == MemoryJobType.EXTRACT_OBJECTS.value,
            self._scope(),
            MemoryJobRow.dedupe_key == "extract_objects:" + ConversationRow.character_id + ":" + SessionRow.session_id,
        ).correlate(SessionRow, ConversationRow).exists()
        return ~registered

    async def read_object_source(self, job: ObjectJob) -> ObjectSourceInput:
        """读取本session完整轮与固定继承上文；继承材料仅供理解指代。"""
        if not isinstance(job, ObjectJob) or len(job.input.source_session_ids) != 1:
            raise InvalidStateError("object来源需要单个session的ObjectJob")
        async with self._database.db_session() as session:
            return await self._load_object_source(session, job)

    async def _load_object_source(self, session: AsyncSession, job: ObjectJob) -> ObjectSourceInput:
        source = await self._read_sealed_session_source(session, job.input.source_session_ids[0])
        if source.character_id != job.input.character_id:
            raise InvalidStateError("object来源session不属于任务登记的角色")
        old_session = await session.get(SessionRow, source.session_id)
        inherited_context, _ = await self._read_inherited_source(session, old_session, source.character_id)
        return ObjectSourceInput(session_id=source.session_id, character_id=source.character_id,
                                 owned_turns=source.turns, inherited_context=inherited_context)

    async def begin_generate(self, job_id: str) -> ObjectJob | None:
        """共用生成入口，只登记代次与执行态，不加载来源。"""
        return cast(ObjectJob | None, await self._begin_generate(job_id, MemoryJobType.EXTRACT_OBJECTS))

    async def save_extraction_result(self, job_id: str, attempt_version: int, objects: tuple[MemoryObject, ...], *,
                             source: ObjectSourceInput, selection_notes: tuple[ObjectSelectionNote, ...] = ()) -> bool:
        """生成结果与固定来源一同落库，空结果也形成恢复点。"""
        manifest = source.source_manifest()
        async with self._database.transaction() as session:
            row, job = await self._read_object_job(session, job_id)
            if job.attempt_version != attempt_version:
                return False
            if job.status != MemoryJobStatus.GENERATING:
                return (job.status in (MemoryJobStatus.NEED_INDEX, MemoryJobStatus.INDEXING,
                                       MemoryJobStatus.NEED_ACTIVATE, MemoryJobStatus.DONE)
                    and job.objects == objects and job.selection_notes == selection_notes
                    and job.source_manifest == manifest)
            if (source.character_id != job.input.character_id
                    or (source.session_id,) != job.input.source_session_ids):
                raise InvalidStateError("object候选来源与任务不一致")
            if any(memory_object.character_id != source.character_id for memory_object in objects):
                raise InvalidStateError("object候选不属于任务角色")
            current_source = await self._load_object_source(session, job)
            if current_source.source_manifest() != manifest:
                raise MemorySourceChangedError("object生成期间来源已变化，需要重新生成")
            now = datetime.now(timezone.utc)
            job = replace(job, objects=objects, selection_notes=selection_notes,
                          source_manifest=manifest, status=MemoryJobStatus.NEED_INDEX,
                          last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True

    async def _read_object_job(self, session: AsyncSession, job_id: str) -> tuple[MemoryJobRow, ObjectJob]:
        row = await session.get(MemoryJobRow, job_id)
        if row is None:
            raise NotFoundError("object任务不存在")
        job = job_mappers.memory_job_to_domain(row)
        if not isinstance(job, ObjectJob):
            raise InvalidStateError("任务不属于object链")
        return row, job

    async def begin_index(self, job_id: str) -> ObjectJob | None:
        """原子领取索引阶段；候选保持不变，重试只重建向量。"""
        async with self._database.transaction() as session:
            row, job = await self._read_object_job(session, job_id)
            now = datetime.now(timezone.utc)
            if job.status != MemoryJobStatus.NEED_INDEX or job.available_at > now:
                return None
            earlier = await session.scalar(select(MemoryJobRow.job_id).where(
                self._scope(), MemoryJobRow.target_key == job.target_key,
                MemoryJobRow.status.not_in((MemoryJobStatus.DONE.value, MemoryJobStatus.FAILED.value,
                                           MemoryJobStatus.OBSOLETE.value)),
                or_(MemoryJobRow.created_at < job.created_at,
                    and_(MemoryJobRow.created_at == job.created_at, MemoryJobRow.job_id < job.job_id)),
            ).limit(1))
            if earlier is not None:
                return None
            if job.source_manifest is None:
                raise InvalidStateError("object索引缺少已保存的生成来源")
            job = replace(job, status=MemoryJobStatus.INDEXING,
                          attempt_version=job.attempt_version + 1, attempt_count=job.attempt_count + 1,
                          last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return job

    async def save_index(self, job_id: str, attempt_version: int) -> bool:
        """仅在Qdrant确认整批写入后提交进度，不保存向量缓存。"""
        async with self._database.transaction() as session:
            row, job = await self._read_object_job(session, job_id)
            if job.attempt_version != attempt_version:
                return False
            if job.status != MemoryJobStatus.INDEXING:
                return job.status in (MemoryJobStatus.NEED_ACTIVATE, MemoryJobStatus.DONE)
            now = datetime.now(timezone.utc)
            job = replace(job, status=MemoryJobStatus.NEED_ACTIVATE,
                          last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True

    async def activate_revision(self, job_id: str, attempt_version: int) -> bool:
        """同一事务发布整批object落库，完成最后任务，登记done"""
        async with self._database.transaction() as session:
            row, job = await self._read_object_job(session, job_id)
            if job.attempt_version != attempt_version:
                return False
            if job.status == MemoryJobStatus.DONE:
                return True
            now = datetime.now(timezone.utc)
            if job.status != MemoryJobStatus.NEED_ACTIVATE or job.available_at > now:
                return False
            # 检查一下是不是所有的落库了，没有落库就落库
            for memory_object in job.objects:
                existing = (await session.scalars(select(MemoryObjectRow).where(or_(
                    MemoryObjectRow.object_id == memory_object.object_id,
                    MemoryObjectRow.source_position_key == memory_object.source_position_key,
                )))).all()
                if not existing:
                    session.add(object_mappers.memory_object_to_row(memory_object))
                elif len(existing) != 1 or object_mappers.memory_object_to_domain(existing[0]) != memory_object:
                    raise IdempotencyConflictError("正式object的ID或来源位置已被不同内容占用")
            job = replace(job, status=MemoryJobStatus.DONE, finished_at=now, updated_at=now,
                          last_error=None, error_code=None)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True
