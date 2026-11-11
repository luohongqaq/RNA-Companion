"""用户记忆查询、原子编辑和后台索引状态的 SQLite 实现。

承担正式文档列表与详情、幂等和版本校验、正文切版、编辑快照保存，以及索引领取、重试和恢复。
保存命令在同一写事务中完成正文与编辑记录更新；该仓储不承接角色、会话或模型设置管理。
"""
from __future__ import annotations
from module.shared.common.errors.base import IdempotencyConflictError, InvalidInputError, InvalidStateError, NotFoundError
from module.shared.common.ids import IdGenerator
from module.memory_management.shared.core.domain.document import DailyMemory, TopicMemory
from module.shared.memory.types.references import MemoryKind
from module.shared.memory.types.retention import RecallTier
from datetime import datetime, timezone
from module.memory_management.daily.core.domain.document import DailyDocument
from module.memory_management.topic.core.domain.document import TopicDocument
from module.user_edit.memory_edit.core.domain.command import SaveMemoryEdit
from module.user_edit.memory_edit.core.domain.document import DailyDocumentContent, TopicDocumentContent
from module.user_edit.memory_edit.core.domain.job import MemoryEdit, MemoryEditStatus
from module.user_edit.memory_edit.core.domain.query import DailyMemoryCursor, DocumentKind, TopicMemoryCursor
from module.user_edit.memory_edit.core.domain.result import (
    DailyMemoryListItem,
    MemoryDetails,
    MemoryEditDetails,
    MemoryEditResult,
    MemoryListItem,
)
from sqlalchemy import and_, or_, select
from module.memory_management.shared.repository import document_mappers
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import DailyMemoryRow, MemoryEditRow, TopicMemoryRow


class SQLiteMemoryEditRepository:
    """用户记忆查询、原子编辑与索引任务状态的 SQLite 实现。"""
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    @staticmethod
    def _memory_table(kind):
        if kind == MemoryKind.DAILY:
            return DailyMemoryRow, DailyMemoryRow.daily_id, document_mappers.daily_memory_to_domain
        if kind == MemoryKind.TOPIC:
            return TopicMemoryRow, TopicMemoryRow.topic_id, document_mappers.topic_memory_to_domain
        raise InvalidInputError("记忆类型必须为daily或topic")

    async def get_memory_for_edit(self, *, character_id: str, kind: DocumentKind,
                                  document_id: str) -> DailyMemory | TopicMemory | None:
        """只给业务服务读取内部来源，用于保留未修改条目的证据。"""
        model, identifier, mapper = self._memory_table(kind)
        async with self._database.db_session() as session:
            row = await session.scalar(select(model).where(identifier == document_id,
                *self._published_memory_conditions(model, character_id)))
            if row is None:
                return None
            memory = mapper(row)
            if memory.document.content_hash() != memory.content_hash:
                raise InvalidStateError("正式记忆正文与hash不一致")
            return memory

    @staticmethod
    def _edit_result(row: MemoryEditRow) -> MemoryEditResult:
        return MemoryEditResult(row.edit_id, row.document_id, MemoryKind(row.kind),
                                row.revision, MemoryEditStatus(row.index_status))

    @staticmethod
    def _edit_details(row: MemoryEditRow) -> MemoryEditDetails:
        return MemoryEditDetails(row.edit_id, row.document_id, MemoryKind(row.kind), row.revision,
            MemoryEditStatus(row.index_status), row.character_id, row.retry_count, row.available_at,
            row.last_error, row.indexed_at, row.created_at, row.updated_at)

    async def get_memory_edit(self, edit_id: str) -> MemoryEditDetails | None:
        async with self._database.db_session() as session:
            row = await session.get(MemoryEditRow, edit_id)
            return self._edit_details(row) if row is not None else None

    async def retry_failed_memory_edit_index(self, edit_id: str) -> MemoryEditDetails:
        """短写事务重新排队，清零本轮自动重试次数，并使旧执行代次失效。"""
        result = None
        async with self._database.transaction() as session:
            row = await session.get(MemoryEditRow, edit_id)
            if row is None:
                raise NotFoundError("编辑记录不存在")
            if row.index_status != "failed":
                raise InvalidStateError("只有失败的编辑索引可以手动重试")
            if await self._edit_is_current(session, row) is not None:
                now = datetime.now(timezone.utc)
                row.index_status, row.retry_count = "pending", 0
                row.attempt_version += 1
                row.available_at, row.updated_at = now, now
                row.last_error, row.indexed_at = None, None
                await session.flush()
                result = self._edit_details(row)
        # 已过期记录的obsolete状态先提交，再返回冲突，不能让异常回滚状态变更。
        if result is None:
            raise InvalidStateError("该编辑已被较新正式版本取代，不能重新索引")
        return result

    @staticmethod
    def _public_document(document):
        if isinstance(document, DailyDocument):
            return DailyDocumentContent.from_document(document)
        return TopicDocumentContent.from_document(document)

    async def save_memory_edit(self, command: SaveMemoryEdit) -> MemoryEditResult:
        """BEGIN IMMEDIATE 内完成幂等、版本检查、正式切版和待索引记录。"""
        model, identifier, _ = self._memory_table(command.kind)
        expected_type = DailyDocument if command.kind == MemoryKind.DAILY else TopicDocument
        if not isinstance(command.document, expected_type):
            raise InvalidInputError("记忆类型与编辑正文不一致")
        async with self._database.transaction() as session:
            existing = await session.scalar(select(MemoryEditRow).where(
                MemoryEditRow.character_id == command.character_id,
                MemoryEditRow.idempotency_key == command.idempotency_key))
            if existing is not None:
                saved = (DailyDocument.from_data(existing.document_json) if existing.kind == "daily"
                         else document_mappers._memory_topic_document_from_json(existing.document_json))
                if ((existing.kind, existing.document_id, existing.base_revision) !=
                        (command.kind, command.document_id, command.expected_revision)
                        or self._public_document(saved) != self._public_document(command.document)):
                    raise IdempotencyConflictError("幂等键已用于其他编辑内容")
                return self._edit_result(existing)
            current = await session.scalar(select(model).where(identifier == command.document_id,
                *self._published_memory_conditions(model, command.character_id)))
            if current is None:
                raise NotFoundError("记忆文档不存在")
            if current.revision != command.expected_revision:
                raise InvalidStateError("记忆版本已变化，请重新读取后提交")
            if command.kind == MemoryKind.DAILY and command.document.memory_date != current.memory_date:
                raise InvalidInputError("不能修改日记归属日期")
            now = datetime.now(timezone.utc)
            revision = current.revision + 1
            content_hash = command.document.content_hash()
            edit = MemoryEditRow(edit_id=IdGenerator.new("memory_edit"), character_id=command.character_id,
                kind=command.kind.value, document_id=command.document_id, idempotency_key=command.idempotency_key,
                base_revision=current.revision, base_content_hash=current.content_hash, revision=revision,
                content_hash=content_hash, document_json=command.document.to_data(), available_at=now,
                created_at=now, updated_at=now, index_status="pending")
            session.add(edit)
            current.document_json = edit.document_json
            current.revision, current.content_hash, current.updated_at = revision, content_hash, now
            # 新版立即正式生效。旧向量保留旧版本/hash，读取端据此排除。
            current.vector_document_key = f"{command.document_id}:r{revision}:{content_hash}"
            if command.kind == MemoryKind.DAILY:
                current.source_session_ids_json = list(command.document.source_session_ids)
            else:
                refs = tuple(dict.fromkeys((*command.document.topic_identity.source_refs,
                    *(ref for entry in command.document.entries for ref in entry.source_refs))))
                current.source_refs_json = [document_mappers._memory_evidence_ref_to_json(ref) for ref in refs]
            await session.flush()
            return self._edit_result(edit)

    async def _edit_is_current(self, session, edit):
        model, _, _ = self._memory_table(edit.kind)
        current = await session.get(model, edit.document_id)
        valid = (current is not None and current.character_id == edit.character_id
                 and (current.revision, current.content_hash) == (edit.revision, edit.content_hash))
        if not valid:
            edit.index_status, edit.indexed_at = "obsolete", None
            edit.attempt_version += 1
            edit.updated_at = datetime.now(timezone.utc)
        return current if valid else None

    async def claim_next_memory_edit(self) -> MemoryEdit | None:
        """原子领取；被后续正式版本替代的记录保留历史，但不再编码。"""
        async with self._database.transaction() as session:
            now = datetime.now(timezone.utc)
            stmt = select(MemoryEditRow).where(MemoryEditRow.index_status == "pending",
                MemoryEditRow.available_at <= now).order_by(MemoryEditRow.created_at, MemoryEditRow.edit_id).limit(1)
            while (row := await session.scalar(stmt)) is not None:
                current = await self._edit_is_current(session, row)
                if current is None:
                    await session.flush()
                    continue
                try:
                    document = (DailyDocument.from_data(row.document_json) if row.kind == "daily"
                                else document_mappers._memory_topic_document_from_json(row.document_json))
                except (ValueError, TypeError, KeyError, InvalidStateError) as error:
                    row.index_status, row.updated_at = "failed", now
                    row.last_error = f"编辑快照结构无效（{type(error).__name__}）"
                    await session.flush()
                    continue
                if document.content_hash() != row.content_hash:
                    row.index_status, row.last_error, row.updated_at = "failed", "编辑快照hash不一致", now
                    await session.flush()
                    continue
                row.index_status, row.updated_at = "indexing", now
                row.attempt_version += 1
                row.last_error = None
                await session.flush()
                return MemoryEdit(row.edit_id, row.character_id, row.document_id, MemoryKind(row.kind),
                    row.revision, row.content_hash, document, RecallTier(current.recall_tier),
                    row.attempt_version, row.retry_count, row.created_at)
            return None

    async def complete_memory_edit_index(self, edit_id: str, attempt_version: int) -> bool:
        async with self._database.transaction() as session:
            row = await session.get(MemoryEditRow, edit_id)
            if row is None or row.attempt_version != attempt_version:
                return False
            if row.index_status == "done":
                return True
            if row.index_status != "indexing":
                return False
            if await self._edit_is_current(session, row) is not None:
                row.index_status, row.indexed_at = "done", datetime.now(timezone.utc)
                row.updated_at, row.last_error = row.indexed_at, None
            return True

    async def retry_memory_edit_index(self, edit_id: str, attempt_version: int, *,
                                      available_at: datetime, error: str) -> bool:
        return await self._finish_edit_failure(edit_id, attempt_version, available_at=available_at, error=error)

    async def fail_memory_edit_index(self, edit_id: str, attempt_version: int, *, error: str) -> bool:
        return await self._finish_edit_failure(edit_id, attempt_version, available_at=None, error=error)

    async def _finish_edit_failure(self, edit_id, attempt_version, *, available_at, error):
        async with self._database.transaction() as session:
            row = await session.get(MemoryEditRow, edit_id)
            if row is None or row.index_status != "indexing" or row.attempt_version != attempt_version:
                return False
            if await self._edit_is_current(session, row) is not None:
                row.index_status = "pending" if available_at is not None else "failed"
                row.last_error, row.updated_at = error, datetime.now(timezone.utc)
                row.attempt_version += 1
                if available_at is not None:
                    row.available_at = available_at
                    row.retry_count += 1
            return True

    async def recover_memory_edit_indexes(self) -> int:
        """仅在旧worker已停止后调用；重启后可重复写同一版本向量。"""
        async with self._database.transaction() as session:
            rows = (await session.scalars(select(MemoryEditRow).where(
                MemoryEditRow.index_status == "indexing"))).all()
            now = datetime.now(timezone.utc)
            for row in rows:
                row.index_status, row.available_at, row.updated_at = "pending", now, now
                row.attempt_version += 1
                row.last_error = "应用重启，恢复未完成的编辑索引"
            return len(rows)

    @staticmethod
    def _published_memory_conditions(model, character_id: str):
        """用户管理查询包含冷热及归档文档；召回池状态不等于文档是否正式发布。"""
        return (
            model.character_id == character_id,
            model.published_at.is_not(None),
            model.document_json.is_not(None),
            model.vector_document_key.is_not(None),
        )

    async def list_daily_memories(
        self, *, character_id: str, limit: int = 20, after: DailyMemoryCursor | None = None,
    ) -> tuple[DailyMemoryListItem, ...]:
        """按日记归属日期从早到晚翻页；编辑日记不会改变列表位置。"""
        if type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError("记忆查询limit必须为1～101")
        stmt = select(DailyMemoryRow.daily_id, DailyMemoryRow.memory_date,
            DailyMemoryRow.document_json["title"].as_string().label("title"),
            DailyMemoryRow.revision, DailyMemoryRow.updated_at).where(
                *self._published_memory_conditions(DailyMemoryRow, character_id))
        if after is not None:
            memory_date, document_id = after
            stmt = stmt.where(or_(DailyMemoryRow.memory_date > memory_date,
                and_(DailyMemoryRow.memory_date == memory_date, DailyMemoryRow.daily_id > document_id)))
        stmt = stmt.order_by(DailyMemoryRow.memory_date.asc(), DailyMemoryRow.daily_id.asc()).limit(limit)
        async with self._database.db_session() as session:
            rows = (await session.execute(stmt)).mappings().all()
            result = []
            for row in rows:
                if not isinstance(row["title"], str) or not row["title"].strip():
                    raise InvalidStateError("正式日记缺少有效标题")
                result.append(DailyMemoryListItem(row["daily_id"], MemoryKind.DAILY, row["title"],
                    row["revision"], row["updated_at"], row["memory_date"]))
            return tuple(result)

    async def list_topic_memories(
        self, *, character_id: str, limit: int = 20, before: TopicMemoryCursor | None = None,
    ) -> tuple[MemoryListItem, ...]:
        """按最近更新时间倒序翻页，同一更新时间按topic_id倒序保证稳定。"""
        if type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError("记忆查询limit必须为1～101")
        stmt = select(TopicMemoryRow.topic_id,
            TopicMemoryRow.document_json["topic"].as_string().label("title"),
            TopicMemoryRow.revision, TopicMemoryRow.updated_at).where(
                *self._published_memory_conditions(TopicMemoryRow, character_id))
        if before is not None:
            timestamp, document_id = before
            stmt = stmt.where(or_(TopicMemoryRow.updated_at < timestamp,
                and_(TopicMemoryRow.updated_at == timestamp, TopicMemoryRow.topic_id < document_id)))
        stmt = stmt.order_by(TopicMemoryRow.updated_at.desc(), TopicMemoryRow.topic_id.desc()).limit(limit)
        async with self._database.db_session() as session:
            rows = (await session.execute(stmt)).mappings().all()
            result = []
            for row in rows:
                if not isinstance(row["title"], str) or not row["title"].strip():
                    raise InvalidStateError("正式主题缺少有效标题")
                result.append(MemoryListItem(row["topic_id"], MemoryKind.TOPIC, row["title"],
                    row["revision"], row["updated_at"]))
            return tuple(result)

    async def get_memory(
        self, *, character_id: str, kind: DocumentKind, document_id: str,
    ) -> MemoryDetails | None:
        """从SQLite正式版本读取正文，核验hash后投影为不含内部证据的详情。"""
        if kind == MemoryKind.DAILY:
            model, identifier, mapper = DailyMemoryRow, DailyMemoryRow.daily_id, document_mappers.daily_memory_to_domain
        elif kind == MemoryKind.TOPIC:
            model, identifier, mapper = TopicMemoryRow, TopicMemoryRow.topic_id, document_mappers.topic_memory_to_domain
        else:
            raise ValueError("记忆查询只支持daily或topic")
        stmt = select(model).where(identifier == document_id,
            *self._published_memory_conditions(model, character_id))
        async with self._database.db_session() as session:
            row = await session.scalar(stmt)
            if row is None:
                return None
            memory = mapper(row)
            if memory.document.content_hash() != memory.content_hash:
                raise InvalidStateError("正式记忆正文与登记hash不一致")
            return MemoryDetails.from_memory(memory, character_id=row.character_id)
