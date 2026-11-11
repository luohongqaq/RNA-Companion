"""用户记忆列表与详情查询服务。

校验角色、数量和文档类别，按 Daily 日期升序或 Topic 更新时间倒序组织分页并返回公开正文。
通过查询端口读取正式版本，不承担修改保存、索引任务或 Agent 检索预算。
"""
from __future__ import annotations
from module.shared.common.errors.base import InvalidInputError, NotFoundError
from module.shared.common.types.pagination import Page
from module.shared.memory.types.references import MemoryKind
from datetime import date, datetime, timezone
from module.user_edit.memory_edit.core.domain.ports import MemoryQueryRepository
from module.user_edit.memory_edit.core.domain.query import DocumentKind
from module.user_edit.memory_edit.core.domain.result import DailyMemoryListItem, MemoryDetails, MemoryListItem
from module.user_edit.memory_edit.core.processing.cursor import MemoryCursorCodec


class MemoryQueryService:
    def __init__(self, repository: MemoryQueryRepository) -> None:
        self._repository = repository

    async def list_daily_memories(
        self, *, character_id: str, limit: int = 20, cursor: str | None = None,
    ) -> Page[DailyMemoryListItem]:
        """日记按归属日期从早到晚展示，游标向更晚的日期推进。"""
        self._validate_list(character_id, limit)
        key = MemoryCursorCodec.decode(cursor, character_id, MemoryKind.DAILY)
        after = None
        if key is not None:
            try:
                after = (date.fromisoformat(key["memory_date"]), key["document_id"])
            except ValueError as exc:
                raise InvalidInputError("日记游标日期无效") from exc
        rows = await self._repository.list_daily_memories(
            character_id=character_id, limit=limit + 1, after=after,
        )
        items = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = MemoryCursorCodec.encode(character_id, MemoryKind.DAILY, {
                "memory_date": last.memory_date.isoformat(), "document_id": last.document_id,
            })
        return Page(items=items, next_cursor=next_cursor)

    async def list_topic_memories(
        self, *, character_id: str, limit: int = 20, cursor: str | None = None,
    ) -> Page[MemoryListItem]:
        """主题按最近更新时间倒序展示，游标向更早的更新时间推进。"""
        self._validate_list(character_id, limit)
        key = MemoryCursorCodec.decode(cursor, character_id, MemoryKind.TOPIC)
        before = None
        if key is not None:
            try:
                timestamp = datetime.fromisoformat(key["updated_at"])
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError
                before = (timestamp.astimezone(timezone.utc), key["document_id"])
            except (ValueError, OverflowError) as exc:
                raise InvalidInputError("主题游标时间无效") from exc
        rows = await self._repository.list_topic_memories(
            character_id=character_id, limit=limit + 1, before=before,
        )
        items = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = MemoryCursorCodec.encode(character_id, MemoryKind.TOPIC, {
                "updated_at": last.updated_at.isoformat(), "document_id": last.document_id,
            })
        return Page(items=items, next_cursor=next_cursor)

    @staticmethod
    def _validate_list(character_id: str, limit: int) -> None:
        if not isinstance(character_id, str) or not 1 <= len(character_id.strip()) <= 128:
            raise InvalidInputError("角色 ID 必须为1～128个字符")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInputError("limit 必须为1～100")

    async def get_memory(
        self, *, character_id: str, kind: DocumentKind, document_id: str,
    ) -> MemoryDetails:
        """读取当前角色的正式文档；不存在、其他角色或尚未发布统一返回不存在。"""
        if not isinstance(character_id, str) or not 1 <= len(character_id.strip()) <= 128:
            raise InvalidInputError("角色 ID 必须为1～128个字符")
        if not isinstance(document_id, str) or not 1 <= len(document_id.strip()) <= 128:
            raise InvalidInputError("文档 ID 必须为1～128个字符")
        if kind not in (MemoryKind.DAILY, MemoryKind.TOPIC):
            raise InvalidInputError("记忆类型必须为daily或topic")
        result = await self._repository.get_memory(
            character_id=character_id, kind=kind, document_id=document_id,
        )
        if result is None:
            raise NotFoundError("记忆文档不存在")
        return result
