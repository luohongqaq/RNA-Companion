"""Topic 记忆的 SQLite 持久化：任务登记、候选保存与发布写入。"""
from __future__ import annotations
from module.shared.common.errors.base import InvalidStateError, NotFoundError
from module.memory_management.shared.core.domain.document import VectorDocument
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier
from module.shared.common.time import BackendTime
from dataclasses import replace
from datetime import datetime, timezone
from module.memory_management.object.core.domain.job import ObjectJob
from module.memory_management.object.core.domain.job import ObjectJobInput
from module.memory_management.object.core.domain.document import MemoryObject, ObjectState
from module.memory_management.shared.contracts.errors import MemorySourceChangedError
from module.memory_management.shared.jobs.core.domain.command import CreateMemoryJob
from module.memory_management.shared.jobs.core.domain.job import MemoryJobStatus, MemoryJobType
from module.memory_management.shared.jobs.repository.sqlite import MemoryJobRepository
from module.memory_management.topic.core.domain.source import TopicContext
from module.memory_management.topic.core.domain.generation import TopicBatchResult
from module.memory_management.topic.core.domain.job import TopicJob, TopicJobInput
from module.memory_management.topic.core.domain.publication import TopicWriteItem
from module.memory_management.topic.core.domain.review import TopicReviewResult
from module.memory_management.topic.core.domain.source import TopicSourceInput
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from module.memory_management.shared.repository import document_mappers
from module.memory_management.shared.jobs.repository import mappers as job_mappers
from module.memory_management.object.repository import mappers as object_mappers
from storage.sqlite.models import ConversationRow, MemoryJobRow, MemoryObjectRow, SessionRow, TopicMemoryRow
from typing import cast


class TopicProductionRepository(MemoryJobRepository):
    job_family = "topic"

    job_class = TopicJob

    async def read_topics(self, topic_ids: tuple[str, ...], *, character_id: str,
                          pools: tuple[RecallTier, ...]) -> tuple[TopicContext, ...]:
        if not topic_ids or not pools:
            return ()
        async with self._database.db_session() as session:
            rows = (await session.scalars(select(TopicMemoryRow).where(
                TopicMemoryRow.topic_id.in_(topic_ids), TopicMemoryRow.character_id == character_id,
                TopicMemoryRow.recall_tier.in_([pool.value for pool in pools]),
                TopicMemoryRow.published_at.is_not(None), TopicMemoryRow.document_json.is_not(None),
                TopicMemoryRow.vector_document_key.is_not(None),
            ))).all()
            by_id = {row.topic_id: row for row in rows}
            results = []
            for topic_id in dict.fromkeys(topic_ids):
                if topic_id not in by_id:
                    continue
                memory = document_mappers.topic_memory_to_domain(by_id[topic_id])
                results.append(TopicContext(MemoryRevisionRef(MemoryKind.TOPIC, topic_id,
                    memory.current_revision, memory.content_hash), memory.document))
            return tuple(results)

    async def read_objects(self, object_ids: tuple[str, ...], *, character_id: str) -> tuple[MemoryObject, ...]:
        if not object_ids:
            return ()
        async with self._database.db_session() as session:
            rows = (await session.scalars(select(MemoryObjectRow).where(
                MemoryObjectRow.character_id == character_id,
                MemoryObjectRow.object_id.in_(object_ids),
            ))).all()
            by_id = {row.object_id: row for row in rows}
            return tuple(object_mappers.memory_object_to_domain(by_id[cid])
                         for cid in dict.fromkeys(object_ids) if cid in by_id)

    def _unregistered_session_condition(self) -> ColumnElement[bool]:
        """查询哪些封存 session 还没登记过 topic 任务"""
        # 先找到这个session下的已完成object_job.id（只要知道dedupe_key就能查到这个job），看一下object任务是否已完成
        completed = select(MemoryJobRow.job_id).where(
            MemoryJobRow.job_type == MemoryJobType.EXTRACT_OBJECTS.value,
            MemoryJobRow.payload_json["job_family"].as_string() == "object",
            MemoryJobRow.status == MemoryJobStatus.DONE.value,
            MemoryJobRow.dedupe_key == "extract_objects:" + ConversationRow.character_id + ":" + SessionRow.session_id,
        ).correlate(SessionRow, ConversationRow).exists()
        # 返回SQL表达式对象：IS NULL 的表达式对象 + Exists 对象
        return SessionRow.topic_jobs_registered_at.is_(None) & completed

    async def read_sealed_session_objects(self, session_id: str) -> tuple[MemoryObject, ...]:
        """根据session_id，读取封存session的object（登记任务前的读取）"""
        async with self._database.db_session() as session:
            return await self._read_sealed_session_objects(session, session_id)

    async def _read_sealed_session_objects(self, session: AsyncSession, session_id: str) -> tuple[MemoryObject, ...]:
        character_id = await self._read_session_character_id(session, session_id)
        command = ObjectJobInput.create_command(character_id, session_id)
        # 找到该session对应的object_job
        row = await session.scalar(select(MemoryJobRow).where(MemoryJobRow.dedupe_key == command.dedupe_key))
        if row is None:
            raise InvalidStateError("session尚无已完成的object提取任务")
        job = job_mappers.memory_job_to_domain(row)
        # 检验一下该object_job是否已完成
        if (not isinstance(job, ObjectJob) or job.status != MemoryJobStatus.DONE
                or job.input.character_id != character_id or job.input.source_session_ids != (session_id,)):
            raise InvalidStateError("session的object提取尚未完成或来源不一致")
        # 拿到该session下的object_id
        ids = tuple(item.object_id for item in job.objects)
        # 去数据库里读取这些object的每一行
        rows = (await session.scalars(select(MemoryObjectRow).where(MemoryObjectRow.object_id.in_(ids)))).all()
        by_id = {item.object_id: item for item in rows}
        if len(by_id) != len(ids) or any(item.character_id != character_id for item in rows):
            raise InvalidStateError("提取任务对应的正式object缺失或角色不一致")
        return tuple(object_mappers.memory_object_to_domain(by_id[cid]) for cid in ids)

    def _validate_create_command(self, command: CreateMemoryJob) -> None:
        """检查topic的创建指令：类型、来源身份及任务键。"""
        source = command.input
        if (command.job_type != MemoryJobType.CONSOLIDATE_TOPIC or not isinstance(source, TopicJobInput)
                or len(source.source_session_ids) != 1 or not source.object_ids or type(command.priority) is not int):
            raise InvalidStateError("topic首次登记需要单个session及非空object批次")
        expected = TopicJobInput.create_command(source.character_id, source.source_session_ids[0],
                                             source.object_ids, priority=command.priority)
        if command != expected:
            raise InvalidStateError("topic首次登记的任务键或输入不规范")

    async def _validate_create_source(self, session: AsyncSession, command: CreateMemoryJob) -> None:
        # create_job复用公共幂等登记；直接登记单批时同样不能绕过来源校验。
        objects = await self._read_sealed_session_objects(session, command.input.source_session_ids[0])
        available = {item.object_id: item for item in objects}
        if any(cid not in available or available[cid].state != ObjectState.STAGED
               or available[cid].character_id != command.input.character_id for cid in command.input.object_ids):
            raise InvalidStateError("topic批次必须来自当前session已发布的待整合object")

    async def register_topic_jobs_for_session(self, session_id: str,
                                              commands: tuple[CreateMemoryJob, ...]) -> tuple[str, ...]:
        """全部批次与首次完成标记原子提交；任一批失败，整批任务和标记一起回滚。"""
        async with self._database.transaction() as session:
            source_session = await session.get(SessionRow, session_id)
            if source_session is None:
                raise NotFoundError("topic首次登记的session不存在")
            if source_session.topic_jobs_registered_at is not None:
                return ()
            objects = await self._read_sealed_session_objects(session, session_id)
            character_id = await self._read_session_character_id(session, session_id)
            # service负责分批，仓储负责核对所有批次合起来没有漏项、重复或混入其他来源。
            for command in commands:
                self._validate_create_command(command)
                if command.input.source_session_ids != (session_id,):
                    raise InvalidStateError("topic首次登记批次混入其他session")
                if command.input.character_id != character_id:
                    raise InvalidStateError("topic首次登记批次角色不一致")
            requested = [cid for command in commands for cid in command.input.object_ids]
            if len(set(requested)) != len(requested) or set(requested) != {item.object_id for item in objects}:
                raise InvalidStateError("topic首次登记未完整且唯一地覆盖本session的object")
            job_ids = []
            for command in commands:
                job_ids.append(await self.create_job_in_transaction(session, command))
            # 空object结果也到这里：确认无需任务后仍写标记，避免永远重复扫描。
            source_session.topic_jobs_registered_at = datetime.now(timezone.utc)
            await session.flush()
            return tuple(job_ids)

    async def read_topic_source(self, job: TopicJob) -> TopicSourceInput:
        raise NotImplementedError("topic.read_topic_source")

    async def begin_generate(self, job_id: str) -> TopicJob | None:
        """复用公共生成入口：核对到期队首，登记尝试代次，不读取或召回材料。"""
        return cast(TopicJob | None, await self._begin_generate(job_id, MemoryJobType.CONSOLIDATE_TOPIC))

    async def save_generation_result(self, job_id: str, attempt_version: int, generation_result: TopicBatchResult, *,
                             source: TopicSourceInput) -> bool:
        """保持topic生成的结果、review的生成恢复点：固定材料、计划、候选和下一状态同事务提交。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("topic任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, TopicJob):
                raise InvalidStateError("任务不属于topic链")
            if job.attempt_version != attempt_version:
                return False
            if not isinstance(source, TopicSourceInput) or not isinstance(generation_result, TopicBatchResult):
                raise InvalidStateError("topic候选保存需要本次生成材料和TopicBatchResult")
            # 核对任务状态是否已经往后推进
            if job.status != MemoryJobStatus.GENERATING:
                return (job.status in (MemoryJobStatus.NEED_REVIEW, MemoryJobStatus.REVIEWING,
                    MemoryJobStatus.NEED_WRITE, MemoryJobStatus.WRITING, MemoryJobStatus.NEED_INDEX,
                    MemoryJobStatus.INDEXING, MemoryJobStatus.WAITING_INDEX,
                    MemoryJobStatus.NEED_ACTIVATE, MemoryJobStatus.DONE)
                    and job.generation_result == generation_result and job.plan == source.plan
                    and job.generation_source == source)
            plan = source.plan
            if plan.character_id != job.input.character_id or plan.object_ids != job.input.object_ids:
                raise InvalidStateError("topic生成材料与任务登记不一致")
            generation_result.validate_against_plan(plan)
            # 核对固定材料object是否发生变化
            objects = [item for group in source.groups for item in (group.object, *group.related_objects)]
            rows = (await session.scalars(select(MemoryObjectRow).where(
                MemoryObjectRow.object_id.in_((*plan.object_ids, *plan.related_object_ids)),
                MemoryObjectRow.character_id == plan.character_id))).all()
            current_objects = {item.object_id: object_mappers.memory_object_to_domain(item) for item in rows}
            if any(item.state != ObjectState.STAGED or current_objects.get(item.object_id) != item
                   for item in objects):
                raise MemorySourceChangedError("topic生成期间object材料已变化，需要重新生成")
            # 核对固定材料topic是否发生变化
            topics = [item for group in source.groups for item in group.existing_topics]
            rows = (await session.scalars(select(TopicMemoryRow).where(
                TopicMemoryRow.topic_id.in_(tuple(ref.memory_id for ref in plan.topic_refs)),
                TopicMemoryRow.character_id == plan.character_id,
                TopicMemoryRow.published_at.is_not(None), TopicMemoryRow.document_json.is_not(None),
                TopicMemoryRow.vector_document_key.is_not(None)))).all()
            current_topics = {item.topic_id: document_mappers.topic_memory_to_domain(item) for item in rows}
            for context in topics:
                current = current_topics.get(context.ref.memory_id)
                if (current is None or current.current_revision != context.ref.revision
                        or current.content_hash != context.ref.content_hash or current.document != context.document):
                    raise MemorySourceChangedError("topic基础版本已变化，需要重新生成")
            now = datetime.now(timezone.utc)
            job = replace(job, plan=plan, generation_source=source, generation_result=generation_result,
                status=MemoryJobStatus.NEED_REVIEW, last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True

    async def begin_review(self, job_id: str) -> TopicJob | None:
        """沿用daily的审核领取方式，读取候选并登记新代次。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("topic任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, TopicJob):
                raise InvalidStateError("任务不属于topic链")
            now = datetime.now(timezone.utc)
            if job.status != MemoryJobStatus.NEED_REVIEW or job.available_at > now:
                return None
            if job.generation_result is None or job.plan is None or job.generation_source is None:
                raise InvalidStateError("topic审核缺少已保存的候选或固定来源")
            earlier = await session.scalar(select(MemoryJobRow.job_id).where(
                self._scope(), MemoryJobRow.target_key == job.target_key,
                MemoryJobRow.status.not_in((MemoryJobStatus.DONE.value, MemoryJobStatus.FAILED.value,
                                            MemoryJobStatus.OBSOLETE.value)),
                or_(MemoryJobRow.created_at < job.created_at,
                    and_(MemoryJobRow.created_at == job.created_at, MemoryJobRow.job_id < job.job_id)),
            ).limit(1))
            if earlier is not None:
                return None
            job = replace(job, status=MemoryJobStatus.REVIEWING,
                attempt_version=job.attempt_version + 1, attempt_count=job.attempt_count + 1,
                last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return job

    async def save_review(self, job_id: str, attempt_version: int, review: TopicReviewResult) -> bool:
        """同事务保存批量审核结论和候选绑定，推进发布准备阶段。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("topic任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, TopicJob) or not isinstance(review, TopicReviewResult):
                raise InvalidStateError("topic审核保存需要TopicJob和TopicReviewResult")
            if job.attempt_version != attempt_version:
                return False
            if job.status != MemoryJobStatus.REVIEWING:
                return job.review == review and job.status in (
                    MemoryJobStatus.NEED_WRITE, MemoryJobStatus.WRITING, MemoryJobStatus.NEED_INDEX,
                    MemoryJobStatus.INDEXING, MemoryJobStatus.WAITING_INDEX,
                    MemoryJobStatus.NEED_ACTIVATE, MemoryJobStatus.DONE)
            if job.generation_result is None:
                raise InvalidStateError("topic审核任务缺少候选")
            review.validate(job.generation_result)
            now = datetime.now(timezone.utc)
            job = replace(job, review=review,
                reviewed_version=job.generation_result.result_version,
                reviewed_hash=job.generation_result.content_hash,
                status=MemoryJobStatus.NEED_WRITE, last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True

    async def begin_write(self, job_id: str) -> TopicJob | None:
        """读取已审核整批申请，登记写入代次；渲染在事务外执行。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("topic任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, TopicJob):
                raise InvalidStateError("任务不属于topic链")
            now = datetime.now(timezone.utc)
            if job.status != MemoryJobStatus.NEED_WRITE or job.available_at > now:
                return None
            job.final_applications()
            earlier = await session.scalar(select(MemoryJobRow.job_id).where(
                self._scope(), MemoryJobRow.target_key == job.target_key,
                MemoryJobRow.status.not_in((MemoryJobStatus.DONE.value, MemoryJobStatus.FAILED.value,
                                            MemoryJobStatus.OBSOLETE.value)),
                or_(MemoryJobRow.created_at < job.created_at,
                    and_(MemoryJobRow.created_at == job.created_at, MemoryJobRow.job_id < job.job_id)),
            ).limit(1))
            if earlier is not None:
                return None
            job = replace(job, status=MemoryJobStatus.WRITING,
                attempt_version=job.attempt_version + 1, attempt_count=job.attempt_count + 1,
                last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return job

    async def save_write(self, job_id: str, attempt_version: int, results: tuple[TopicWriteItem, ...]) -> bool:
        """沿用daily的写入恢复点，整批校验并保存；不覆盖正式topic。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("topic任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, TopicJob) or not isinstance(results, tuple) or any(
                    not isinstance(item, TopicWriteItem) for item in results):
                raise InvalidStateError("topic发布准备需要整批TopicWriteItem")
            if job.attempt_version != attempt_version:
                return False
            if job.status != MemoryJobStatus.WRITING:
                return job.write_results == results and job.status in (
                    MemoryJobStatus.NEED_INDEX, MemoryJobStatus.INDEXING, MemoryJobStatus.WAITING_INDEX,
                    MemoryJobStatus.NEED_ACTIVATE, MemoryJobStatus.DONE)
            applications = job.final_applications()
            if len(results) != len(applications):
                raise InvalidStateError("topic待发布结果未完整覆盖审核通过的申请")
            for result, app in zip(results, applications, strict=True):
                base = app.base_ref
                if (result.application_id != app.application_id or result.ref.memory_id != app.topic_id
                        or result.ref.revision != (base.revision + 1 if base is not None else 1)
                        or result.write.document != app.document
                        or result.write.content_hash != app.document.content_hash()):
                    raise InvalidStateError("topic待发布身份、版本或正文与审核结果不一致")
            now = datetime.now(timezone.utc)
            job = replace(job, write_results=results, status=MemoryJobStatus.NEED_INDEX,
                          last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True

    async def begin_index(self, job_id: str) -> TopicJob | None:
        """登记整批索引代次；向量化和外部写入在事务外执行。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("topic任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, TopicJob):
                raise InvalidStateError("任务不属于topic链")
            now = datetime.now(timezone.utc)
            if job.status != MemoryJobStatus.NEED_INDEX or job.available_at > now:
                return None
            job.index_documents()
            earlier = await session.scalar(select(MemoryJobRow.job_id).where(
                self._scope(), MemoryJobRow.target_key == job.target_key,
                MemoryJobRow.status.not_in((MemoryJobStatus.DONE.value, MemoryJobStatus.FAILED.value,
                                            MemoryJobStatus.OBSOLETE.value)),
                or_(MemoryJobRow.created_at < job.created_at,
                    and_(MemoryJobRow.created_at == job.created_at, MemoryJobRow.job_id < job.job_id)),
            ).limit(1))
            if earlier is not None:
                return None
            job = replace(job, status=MemoryJobStatus.INDEXING,
                attempt_version=job.attempt_version + 1, attempt_count=job.attempt_count + 1,
                last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return job

    async def save_index(self, job_id: str, attempt_version: int, documents: tuple[VectorDocument, ...]) -> bool:
        """整批外部索引确认后保存进度；正文和固定版本仍复用write_results。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("topic任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, TopicJob):
                raise InvalidStateError("任务不属于topic链")
            if job.attempt_version != attempt_version:
                return False
            if job.status != MemoryJobStatus.INDEXING:
                return (job.status in (MemoryJobStatus.NEED_ACTIVATE, MemoryJobStatus.DONE)
                        and documents == job.index_documents())
            if documents != job.index_documents():
                raise InvalidStateError("topic索引结果未完整对应待发布批次")
            now = datetime.now(timezone.utc)
            job = replace(job, status=MemoryJobStatus.NEED_ACTIVATE,
                          last_error=None, error_code=None, updated_at=now)
            row.status, row.updated_at = job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(job)
            await session.flush()
            return True

    async def prepare_activation(self, job_id: str, attempt_version: int) -> TopicJob | None:
        """提交整批正式版及object关联，保留NEED_ACTIVATE供事务外同步Qdrant。

        中断后认领同一批published_refs，不重复升版；完成外部元数据同步后再标记DONE。
        """
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("topic任务不存在")
            job = job_mappers.memory_job_to_domain(row)
            if not isinstance(job, TopicJob):
                raise InvalidStateError("任务不属于topic链")
            if job.attempt_version != attempt_version:
                return None
            if job.status == MemoryJobStatus.DONE:
                return None
            if job.status != MemoryJobStatus.NEED_ACTIVATE or job.available_at > datetime.now(timezone.utc):
                return None
            # 校验待发布正文与审核结果，复用索引阶段使用的稳定ID、版本和hash。
            documents = job.index_documents()
            refs = tuple(document.ref for document in documents)
            if job.published_refs is not None and job.published_refs != refs:
                raise InvalidStateError("topic已发布版本与待发布批次不一致")
            applications = job.final_applications()
            now = datetime.now(timezone.utc)
            # 整批在同一事务内切版；任一topic或object冲突，整批回滚。
            for app, item, document in zip(applications, job.write_results, documents, strict=True):
                current = await session.get(TopicMemoryRow, item.ref.memory_id)
                if current is not None and current.character_id != job.input.character_id:
                    raise MemorySourceChangedError("topic ID已属于其他角色")
                current_ref = (MemoryRevisionRef(MemoryKind.TOPIC, current.topic_id,
                    current.revision, current.content_hash) if current is not None else None)
                if job.published_refs is not None:
                    # published_refs不为None表示正式版已提交，可能中断于外部元数据同步；只能认领同一版本。
                    if current_ref != item.ref:
                        raise MemorySourceChangedError("已发布topic发生变化，不能覆盖较新正文")
                else:
                    # CAS：当前正式版必须仍等于申请固定的base_ref；新建时双方都应为None。
                    if current_ref != app.base_ref:
                        raise MemorySourceChangedError("正式topic基础版本已变化，需要重新生成")
                    # 汇总主题说明和时间线的证据，去重后随完整结构化正文保存。
                    evidence = tuple(dict.fromkeys((*item.write.document.topic_identity.source_refs,
                        *(ref for event in item.write.document.entries for ref in event.source_refs))))
                    # 新增或更新ORM记录，由当前事务统一提交。
                    values = dict(character_id=job.input.character_id, revision=item.ref.revision,
                        content_hash=item.ref.content_hash, document_json=item.write.document.to_data(),
                        source_refs_json=[document_mappers._memory_evidence_ref_to_json(ref) for ref in evidence],
                        vector_document_key=document.vector_document_key, published_at=now,
                        recall_tier=job.input.recall_tier.value, updated_at=now)
                    if current is None:
                        # 第一版topic直接新增。
                        session.add(TopicMemoryRow(topic_id=item.ref.memory_id, created_at=now,
                                                   last_mentioned_at=BackendTime.local(now).date(), **values))
                    else:
                        # 更新已有topic，保留原始created_at。
                        if current.last_mentioned_at is None:
                            current.last_mentioned_at = BackendTime.local(now).date()
                        for key, value in values.items():
                            setattr(current, key, value)
                # 仅关联审核通过申请采用的object；暂存或拒绝申请的object保持原状。
                for object_id in app.object_ids:
                    obj = await session.get(MemoryObjectRow, object_id)
                    if obj is None or obj.character_id != job.input.character_id:
                        raise MemorySourceChangedError("topic采用的object缺失或角色不一致")
                    if job.published_refs is not None:
                        # 激活重试时只核对已提交的关联，不重复处理object。
                        if obj.object_state != ObjectState.LINKED.value or obj.linked_topic_id != app.topic_id:
                            raise MemorySourceChangedError("已发布topic的object关联已变化")
                    else:
                        # 首次发布只接收尚未整合的object，防止覆盖其他topic的关联。
                        if obj.object_state != ObjectState.STAGED.value or obj.linked_topic_id is not None:
                            raise MemorySourceChangedError("topic采用的object已被处理，需要重新生成")
                        obj.object_state, obj.linked_topic_id = ObjectState.LINKED.value, app.topic_id
            # 正式版与关联全部成功后，同事务保存整批发布恢复点；空批次保存空元组。
            if job.published_refs is None:
                job = replace(job, published_refs=refs, updated_at=now)
                row.updated_at, row.payload_json = now, job_mappers.memory_job_payload_to_json(job)
                await session.flush()
        return job

    async def activate_revision(self, job_id: str, attempt_version: int) -> bool:
        """由service确认Qdrant元数据同步成功后，核对正式版本并将整批任务标记DONE。"""
        async with self._database.transaction() as session:
            row = await session.get(MemoryJobRow, job_id)
            if row is None:
                raise NotFoundError("topic任务不存在")
            current_job = job_mappers.memory_job_to_domain(row)
            if not isinstance(current_job, TopicJob):
                raise InvalidStateError("任务不属于topic链")
            # 检查任务状态，看看是不是已经完成，否则意味着任务失效了
            if current_job.attempt_version != attempt_version or current_job.status != MemoryJobStatus.NEED_ACTIVATE:
                return current_job.status == MemoryJobStatus.DONE and current_job.attempt_version == attempt_version
            job = current_job
            refs = tuple(document.ref for document in job.index_documents())
            if job.published_refs != refs:
                raise InvalidStateError("topic正式版本尚未提交或与待发布批次不一致")
            # 核对整批正式版本，避免在版本变化后完成旧任务。
            for ref in refs:
                current = await session.get(TopicMemoryRow, ref.memory_id)
                if (current is None or current.character_id != job.input.character_id
                        or (current.revision, current.content_hash) != (ref.revision, ref.content_hash)):
                    raise MemorySourceChangedError("任务完成前topic版本已变化")
            now = datetime.now(timezone.utc)
            # 外部同步成功且正式版本仍一致，整批完成任务。
            current_job = replace(current_job, status=MemoryJobStatus.DONE,
                last_error=None, error_code=None, finished_at=now, updated_at=now)
            row.status, row.updated_at = current_job.status.value, now
            row.payload_json = job_mappers.memory_job_payload_to_json(current_job)
            await session.flush()
            return True
