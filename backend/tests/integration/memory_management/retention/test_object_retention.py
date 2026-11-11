"""object留存：未并入topic且允许淡出的object按得知日退出热池。"""
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient, models

from module.memory_management.object.core.domain.document import MemoryObject, ObjectEvent, ObjectState
from module.memory_management.object.repository.mappers import memory_object_to_row
from module.memory_management.retention.core.processing.service import MemoryRetentionService
from module.memory_management.retention.repository.sqlite import SQLiteMemoryRetentionRepository
from module.memory_management.shared.indexing.repository.qdrant import QdrantMemoryIndexRepository
from module.shared.memory.types.references import EvidenceRef
from module.shared.memory.types.retention import RecallTier
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow, MemoryObjectRow

TODAY = date(2026, 11, 11)


@pytest_asyncio.fixture
async def database(tmp_path):
    database = SQLiteDatabase(tmp_path / "object.sqlite3")
    try:
        await database.initialize()
        async with database.transaction() as session:
            session.add(CharacterRow(character_id="c", name="c", character_setting="test"))
        yield database
    finally:
        await database.close()


def memory_object(object_id="obj1", **changes):
    now = datetime(2026, 11, 1, 8, tzinfo=timezone.utc)
    values = dict(
        object_id=object_id, character_id="c", object=f"事情-{object_id}",
        events=(ObjectEvent(actor="user", statement="用户在做桌宠项目",
                            acquire_time=date(2026, 10, 12), occurrence_time=None,
                            source_refs=(EvidenceRef("s1", "m1", "用户在做桌宠项目"),)),),
        source_position_key=f"extract_objects:c:{object_id}:0",
        created_at=now, forgetfulness=1,
    )
    values.update(changes)
    return MemoryObject(**values)


class FakeIndex:
    """只记录降池请求；missing用于模拟索引缺分区。"""

    def __init__(self, missing=()):
        self.missing = set(missing)
        self.calls = []

    async def set_object_pool(self, character_id, object_id, tier):
        self.calls.append((character_id, object_id, tier.value))
        return object_id not in self.missing


@pytest.mark.asyncio
async def test_only_expired_unlinked_objects_leave_hot_pool(database):
    fresh = memory_object(
        "fresh",
        events=(ObjectEvent(actor="user", statement="用户刚换了新工作",
                            acquire_time=date(2026, 11, 2), occurrence_time=None,
                            source_refs=(EvidenceRef("s2", "m2", "用户刚换了新工作"),)),),
    )
    async with database.transaction() as session:
        session.add_all([
            memory_object_to_row(memory_object("expired")),
            memory_object_to_row(memory_object("important", forgetfulness=0)),
            memory_object_to_row(memory_object("linked", state=ObjectState.LINKED, linked_topic_id="topic1")),
            memory_object_to_row(fresh),
        ])
    index = FakeIndex()
    service = MemoryRetentionService(SQLiteMemoryRetentionRepository(database), index)

    assert await service.cool_unlinked_objects(TODAY) == 1
    assert index.calls == [("c", "expired", "cold")]
    async with database.db_session() as session:
        for identifier, expected in (("expired", "cold"), ("important", "hot"),
                                     ("linked", "hot"), ("fresh", "hot")):
            assert (await session.get(MemoryObjectRow, identifier)).recall_tier == expected
    assert await service.cool_unlinked_objects(TODAY) == 0


@pytest.mark.asyncio
async def test_missing_index_point_defers_database_change(database):
    async with database.transaction() as session:
        session.add(memory_object_to_row(memory_object("expired")))
    index = FakeIndex(missing={"expired"})
    service = MemoryRetentionService(SQLiteMemoryRetentionRepository(database), index)

    assert await service.cool_unlinked_objects(TODAY) == 0
    async with database.db_session() as session:
        assert (await session.get(MemoryObjectRow, "expired")).recall_tier == "hot"


@pytest.mark.asyncio
async def test_set_object_pool_updates_only_matching_point(tmp_path):
    client = AsyncQdrantClient(path=str(tmp_path / "qdrant"))
    try:
        index = QdrantMemoryIndexRepository(client, None)
        collection = index.object_collection_name
        await client.create_collection(collection, vectors_config={
            "dense": models.VectorParams(size=4, distance=models.Distance.COSINE)})
        await client.upsert(collection, [
            models.PointStruct(id=1, vector={"dense": [0.1, 0.2, 0.3, 0.4]},
                               payload={"object_id": "obj1", "character_id": "c", "kind": "object", "pool": "hot"}),
            models.PointStruct(id=2, vector={"dense": [0.4, 0.3, 0.2, 0.1]},
                               payload={"object_id": "obj2", "character_id": "c", "kind": "object", "pool": "hot"}),
        ], wait=True)

        assert await index.set_object_pool("c", "obj1", RecallTier.COLD) is True
        assert await index.set_object_pool("c", "missing", RecallTier.COLD) is False
        stored = await client.retrieve(collection, [1, 2])
        assert {point.id: point.payload["pool"] for point in stored} == {1: "cold", 2: "hot"}
    finally:
        await client.close()


@pytest.mark.parametrize("elapsed,expected", [(23, RecallTier.HOT), (24, RecallTier.COLD)])
def test_decay_boundary_matches_daily_and_topic(elapsed, expected):
    """object与daily、topic共用同一阈值：第24个整日离开热池。"""
    _, actual = MemoryRetentionService._evaluate_retention(TODAY - timedelta(days=elapsed), TODAY)
    assert actual is expected
