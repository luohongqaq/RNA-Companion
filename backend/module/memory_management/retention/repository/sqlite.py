"""SQLite 正式记忆与object的使用日期和召回池写入。"""

from datetime import date

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from module.memory_management.object.core.domain.document import ObjectState
from module.memory_management.object.repository.mappers import memory_object_to_domain
from module.memory_management.retention.core.domain.retention import HotMemory, StaleObject
from module.memory_management.shared.contracts.errors import MemorySourceChangedError
from module.shared.common.time import BackendTime
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import DailyMemoryRow, MemoryObjectRow, TopicMemoryRow


class SQLiteMemoryRetentionRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def list_hot_memories(self) -> tuple[HotMemory, ...]:
        memories = []
        async with self._database.db_session() as session:
            for kind, row, identifier in (
                (MemoryKind.DAILY, DailyMemoryRow, DailyMemoryRow.daily_id),
                (MemoryKind.TOPIC, TopicMemoryRow, TopicMemoryRow.topic_id),
            ):
                records = await session.execute(
                    select(identifier, row.character_id, row.revision, row.content_hash, row.last_mentioned_at)
                    .where(
                        row.last_mentioned_at.is_not(None),
                        row.published_at.is_not(None),
                        row.document_json.is_not(None),
                        row.vector_document_key.is_not(None),
                        row.recall_tier == RecallTier.HOT,
                    )
                    .order_by(identifier)
                )
                memories.extend(
                    HotMemory(owner, MemoryRevisionRef(kind, key, revision, digest), mentioned_on)
                    for key, owner, revision, digest, mentioned_on in records
                )
        return tuple(memories)

    async def cool_down_batch(self, memories: tuple[HotMemory, ...]) -> None:
        async with self._database.transaction() as session:
            for memory in memories:
                ref = memory.ref
                if ref.kind == MemoryKind.DAILY:
                    row, identifier = DailyMemoryRow, DailyMemoryRow.daily_id
                else:
                    row, identifier = TopicMemoryRow, TopicMemoryRow.topic_id
                result = await session.execute(
                    update(row)
                    .where(
                        identifier == ref.memory_id,
                        row.character_id == memory.character_id,
                        row.revision == ref.revision,
                        row.content_hash == ref.content_hash,
                        row.last_mentioned_at == memory.last_mentioned_at,
                        row.published_at.is_not(None),
                        row.document_json.is_not(None),
                        row.vector_document_key.is_not(None),
                        row.recall_tier == RecallTier.HOT,
                    )
                    .values(recall_tier=RecallTier.COLD)
                )
                if result.rowcount != 1:
                    raise MemorySourceChangedError("待降池记忆已变化")

    @staticmethod
    async def record_memory_use_in_transaction(
        session: AsyncSession,
        *,
        character_id: str,
        refs: tuple[MemoryRevisionRef, ...],
        used_on: date,
    ) -> None:
        """只续期与冻结引用一致的正式版本，日期只能向前推进。"""
        for ref in dict.fromkeys(refs):
            if ref.kind == MemoryKind.DAILY:
                row, identifier = DailyMemoryRow, DailyMemoryRow.daily_id
            elif ref.kind == MemoryKind.TOPIC:
                row, identifier = TopicMemoryRow, TopicMemoryRow.topic_id
            else:
                continue
            await session.execute(
                update(row)
                .where(
                    identifier == ref.memory_id,
                    row.character_id == character_id,
                    row.revision == ref.revision,
                    row.content_hash == ref.content_hash,
                    row.published_at.is_not(None),
                    row.document_json.is_not(None),
                    row.vector_document_key.is_not(None),
                    or_(row.last_mentioned_at.is_(None), row.last_mentioned_at < used_on),
                )
                .values(last_mentioned_at=used_on)
            )

    async def list_hot_unlinked_objects(self) -> tuple[StaleObject, ...]:
        """读取热池里未并入topic且允许淡出的object；年龄取得知日，不读入库时间。"""
        async with self._database.db_session() as session:
            rows = (await session.scalars(
                select(MemoryObjectRow).where(
                    MemoryObjectRow.recall_tier == RecallTier.HOT,
                    MemoryObjectRow.object_state == ObjectState.STAGED.value,
                    MemoryObjectRow.linked_topic_id.is_(None),
                    MemoryObjectRow.forgetfulness == 1,
                ).order_by(MemoryObjectRow.object_id)
            )).all()
        objects = []
        for row in rows:
            memory = memory_object_to_domain(row)
            known = [event.acquire_time for event in memory.events]
            known.extend(plan.acquire_time for plan in memory.plans)
            mentioned_on = max(known) if known else BackendTime.local(memory.created_at).date()
            objects.append(StaleObject(memory.character_id, memory.object_id, mentioned_on))
        return tuple(objects)

    async def cool_down_objects(self, objects: tuple[StaleObject, ...]) -> None:
        async with self._database.transaction() as session:
            for item in objects:
                result = await session.execute(
                    update(MemoryObjectRow)
                    .where(
                        MemoryObjectRow.object_id == item.object_id,
                        MemoryObjectRow.character_id == item.character_id,
                        MemoryObjectRow.recall_tier == RecallTier.HOT,
                        MemoryObjectRow.object_state == ObjectState.STAGED.value,
                        MemoryObjectRow.linked_topic_id.is_(None),
                        MemoryObjectRow.forgetfulness == 1,
                    )
                    .values(recall_tier=RecallTier.COLD)
                )
                if result.rowcount != 1:
                    raise MemorySourceChangedError("待降池object已变化")
