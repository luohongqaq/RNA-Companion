"""读取production已发布的正式正文；同一快照核对角色、冷热池和版本hash。"""
from __future__ import annotations
from module.shared.common.errors.base import InvalidStateError
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier
from module.memory_recall.retrieval.core.domain.document import RecallDocument
from sqlalchemy import select
from module.memory_management.shared.repository import document_mappers
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import DailyMemoryRow, TopicMemoryRow


class SQLiteMemoryDocumentRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def load(self, character_id: str) -> tuple[RecallDocument, ...]:
        result = []
        async with self._database.db_session() as session:
            for model, mapper, kind in ((DailyMemoryRow, document_mappers.daily_memory_to_domain, MemoryKind.DAILY),
                                        (TopicMemoryRow, document_mappers.topic_memory_to_domain, MemoryKind.TOPIC)):
                rows = (await session.scalars(select(model).where(
                    model.character_id == character_id,
                    model.recall_tier.in_((RecallTier.HOT.value, RecallTier.COLD.value)),
                    model.published_at.is_not(None), model.document_json.is_not(None),
                    model.vector_document_key.is_not(None),
                ))).all()
                for row in rows:
                    memory = mapper(row)
                    if memory.document.content_hash() != memory.content_hash:
                        raise InvalidStateError("正式记忆结构化正文与登记hash不一致")
                    result.append(RecallDocument(MemoryRevisionRef(kind, memory.document_id,
                        memory.current_revision, memory.content_hash), character_id,
                        RecallTier(row.recall_tier), memory.document))
        # 句柄账本使用统一document_id；禁止两种记忆使用同一ID造成材料覆盖。
        if len({item.ref.memory_id for item in result}) != len(result):
            raise InvalidStateError("daily与topic的正式文档ID冲突")
        return tuple(result)
