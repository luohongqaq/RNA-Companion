"""正式daily元数据：日期唯一性、ORM映射和缓存更新信号。"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from module.memory_management.shared.core.domain.document import DailyMemory
from module.shared.common.time import BackendTime
from qdrant_client import AsyncQdrantClient, models
from module.memory_management.retention.core.processing.service import MemoryRetentionService
from module.memory_management.shared.indexing.repository.qdrant import QdrantMemoryIndexRepository
from module.memory_management.retention.repository.sqlite import SQLiteMemoryRetentionRepository
from module.memory_management.daily.core.domain.document import DailyDocument, DailyMoment, MomentKind, DailyReflection
from module.shared.memory.types.references import MemoryKind
from module.shared.memory.types.retention import RecallTier
from module.memory_management.shared.repository import document_mappers
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow, DailyMemoryRow, TopicMemoryRow
from storage.sqlite.seed_memories import SQLiteSeedMemoryRepository


@pytest_asyncio.fixture
async def database(tmp_path):
    database = SQLiteDatabase(tmp_path / "daily.sqlite3")
    try:
        await database.initialize()
        async with database.transaction() as session:
            session.add_all([CharacterRow(character_id=cid, name=cid, character_setting="test") for cid in ("c", "other")])
        yield database
    finally:
        await database.close()


def memory(document_id="daily", **changes):
    now = datetime(2026, 10, 30, 8, tzinfo=timezone.utc)
    memory_date = changes.get("memory_date", date(2026, 10, 30))
    sources = changes.get("source_session_ids", ("s1", "s2"))
    return replace(DailyMemory(document_id=document_id, kind=MemoryKind.DAILY,
        current_revision=2, content_hash="hash", vector_document_key="vector:daily:2",
        published_at=now, updated_at=now,
        source_session_ids=sources, memory_date=memory_date,
        document=DailyDocument(memory_date, sources, "项目安排",
            (DailyMoment(MomentKind.EMOTIONAL_ARC, "用户期待周末完成项目", ("turn",)),),
            reflections=(DailyReflection("我想更认真地听他说。", ("turn",)),))), **changes)


@pytest.mark.asyncio
async def test_daily_memory_roundtrip_without_export_path(database):
    value = memory()
    async with database.transaction() as session:
        session.add(document_mappers.daily_memory_to_row(value, character_id="c", recall_tier=RecallTier.COLD))
    async with database.db_session() as session:
        row = await session.get(DailyMemoryRow, value.document_id)
        assert document_mappers.daily_memory_to_domain(row) == value
        assert type(row.memory_date) is date and row.source_session_ids_json == ["s1", "s2"]
        assert row.document_json["memorable_moments"][0]["text"] == value.document.memorable_moments[0].text
        assert row.document_json["reflections"][0]["text"] == "我想更认真地听他说。"
        assert row.character_id == "c"
        assert row.recall_tier == "cold" and row.created_at == value.published_at
        columns = (await session.execute(text("PRAGMA table_info(daily_memories)"))).mappings().all()
        assert {"status", "index_status"}.isdisjoint(column["name"] for column in columns)


@pytest.mark.asyncio
async def test_one_daily_per_character_date_but_many_source_sessions(database):
    async with database.transaction() as session:
        session.add(document_mappers.daily_memory_to_row(memory(), character_id="c"))
        session.add(document_mappers.daily_memory_to_row(memory("other-owner"), character_id="other"))
        session.add(document_mappers.daily_memory_to_row(memory("next-day", memory_date=date(2026, 10, 31)), character_id="c"))
    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            session.add(document_mappers.daily_memory_to_row(memory("duplicate"), character_id="c"))


@pytest.mark.asyncio
async def test_publication_revision_and_delete_emit_signals_export_does_not(database):
    value = memory()
    async with database.transaction() as session:
        session.add(document_mappers.daily_memory_to_row(value, character_id="c"))
    async with database.db_session() as session:
        assert await session.scalar(text("SELECT deleted FROM memory_update_signals WHERE document_id='daily'")) == 0
        before = await session.scalar(text("SELECT seq FROM memory_update_clock"))
    async with database.db_session() as session:
        assert await session.scalar(text("SELECT seq FROM memory_update_clock")) == before
    async with database.transaction() as session:
        row = await session.get(DailyMemoryRow, "daily")
        row.revision, row.content_hash = 3, "new-hash"
    async with database.db_session() as session:
        signal = (await session.execute(text("SELECT revision, content_hash FROM memory_update_signals WHERE document_id='daily'"))).one()
        assert tuple(signal) == (3, "new-hash")
    async with database.transaction() as session:
        await session.delete(await session.get(DailyMemoryRow, "daily"))
    async with database.db_session() as session:
        assert await session.scalar(text("SELECT deleted FROM memory_update_signals WHERE document_id='daily'")) == 1


def test_daily_mapper_preserves_storage_metadata_and_rejects_wrong_kind():
    value = memory()
    row = document_mappers.daily_memory_to_row(value, character_id="c",
        created_at=datetime(2026, 10, 29, tzinfo=timezone.utc))
    assert document_mappers.daily_memory_to_domain(row) == value
    assert row.created_at < row.published_at
    with pytest.raises(ValueError):
        document_mappers.daily_memory_to_row(replace(value, kind=MemoryKind.TOPIC), character_id="c")


@pytest.mark.asyncio
async def test_seed_publication_uses_current_daily_schema(database):
    repository = SQLiteSeedMemoryRepository(database)
    doc = dict(document_id="seed", kind="daily", revision=1, content_hash="seed-hash", conversation_time="2026-10-29T16:01:00+00:00",
        title="种子记忆", sections={"memorable_moments": [{"kind": "milestone", "text": "种子正文", "source_turn_ids": []}], "profile_facts": [], "intentions": [], "commitments": [], "reflections": []})
    assert (await repository.publish_seed((doc,), "c", "test"))["created"] == 1
    async with database.transaction() as session:
        row = await session.get(DailyMemoryRow, "seed")
        assert row.last_mentioned_at == BackendTime.local(row.published_at).date()
        row.last_mentioned_at = date(2026, 10, 29)
    assert (await repository.publish_seed((doc,), "c", "test"))["unchanged"] == 1
    assert (await repository.publish_seed((dict(doc, revision=2, content_hash="new"),), "c", "test"))["updated"] == 1
    assert await repository.current_versions(("seed",), "c") == {"seed": (2, "new")}
    async with database.db_session() as session:
        row = await session.get(DailyMemoryRow, "seed")
        assert row.memory_date == date(2026, 10, 30) and len(row.source_session_ids_json) == 1
        assert row.vector_document_key == "seed"
        assert row.last_mentioned_at == date(2026, 10, 29)
        assert document_mappers.daily_memory_to_domain(row).document.memorable_moments[0].text == "种子正文"


@pytest.mark.asyncio
async def test_baseline_reinitialize_preserves_published_daily(database):
    value = memory()
    async with database.transaction() as session:
        session.add(document_mappers.daily_memory_to_row(value, character_id="c"))
    await database.initialize()
    async with database.db_session() as session:
        assert document_mappers.daily_memory_to_domain(await session.get(DailyMemoryRow, value.document_id)) == value
        assert list((await session.scalars(text("SELECT version FROM schema_migrations ORDER BY version"))).all()) == [1, 2, 3, 4, 5, 6, 7]
        assert not (await session.execute(text("PRAGMA foreign_key_check"))).all()


@pytest.mark.parametrize(("elapsed", "score", "tier"), [
    (0, 1.0, RecallTier.HOT),
    (7, 0.5, RecallTier.HOT),
    (23, None, RecallTier.HOT),
    (24, None, RecallTier.COLD),
])
def test_retention_by_shanghai_calendar_day(elapsed, score, tier):
    first = BackendTime.local(datetime(2026, 10, 29, 16, tzinfo=timezone.utc)).date()
    assert first == date(2026, 10, 30)
    actual_score, actual_tier = MemoryRetentionService._evaluate_retention(first, first + timedelta(days=elapsed))
    assert actual_tier == tier
    if score is not None:
        assert actual_score == score


@pytest.mark.asyncio
async def test_retention_migration_backfills_published_documents_only(database):
    daily = document_mappers.daily_memory_to_row(memory(), character_id="c", recall_tier=RecallTier.COLD)
    async with database.transaction() as session:
        session.add(daily)
        for topic_id, published_at in (("published", daily.published_at.isoformat()), ("placeholder", None)):
            await session.execute(text(
                "INSERT INTO topic_memories (topic_id, character_id, revision, content_hash, recall_tier, "
                "document_json, vector_document_key, published_at, created_at, updated_at) "
                "VALUES (:id, 'c', 1, 'hash', 'hot', '{}', 'key', :published, :now, :now)"
            ), {"id": topic_id, "published": published_at, "now": daily.published_at.isoformat()})
        await session.flush()
        await session.execute(text("ALTER TABLE daily_memories DROP COLUMN last_mentioned_at"))
        await session.execute(text("ALTER TABLE topic_memories DROP COLUMN last_mentioned_at"))
        await session.execute(text("DELETE FROM schema_migrations WHERE version = 5"))

    first_day = BackendTime.local(datetime.now(timezone.utc)).date()
    await database.initialize()
    await database.initialize()
    last_day = BackendTime.local(datetime.now(timezone.utc)).date()
    async with database.db_session() as session:
        row = await session.get(DailyMemoryRow, "daily")
        assert first_day <= row.last_mentioned_at <= last_day
        assert row.recall_tier == RecallTier.COLD.value
        dates = dict((await session.execute(text(
            "SELECT topic_id, last_mentioned_at FROM topic_memories"
        ))).all())
        assert dates == {"published": row.last_mentioned_at.isoformat(), "placeholder": None}


@pytest.mark.asyncio
async def test_startup_cooldown_retries_partial_index_update(database, tmp_path, monkeypatch):
    today = date(2026, 10, 31)
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    async with database.transaction() as session:
        for offset, (identifier, tier, elapsed) in enumerate((
            ("expired-daily", RecallTier.HOT, 24),
            ("still-hot", RecallTier.HOT, 23),
            ("cold", RecallTier.COLD, 0),
            ("archived", RecallTier.ARCHIVED, 100),
            ("missing-index", RecallTier.HOT, 30),
        )):
            value = memory(identifier, memory_date=today - timedelta(days=offset))
            row = document_mappers.daily_memory_to_row(value, character_id="c", recall_tier=tier)
            row.last_mentioned_at = today - timedelta(days=elapsed)
            session.add(row)
        session.add(TopicMemoryRow(
            topic_id="expired-topic", character_id="c", revision=2, content_hash="hash",
            document_json={}, vector_document_key="topic-key", published_at=now,
            recall_tier="hot", last_mentioned_at=today - timedelta(days=24),
            created_at=now, updated_at=now,
        ))

    path = str(tmp_path / "retention-qdrant")
    client = AsyncQdrantClient(path=path)
    try:
        index = QdrantMemoryIndexRepository(client, None)
        await client.create_collection(index.collection_name,
            vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
        points = [
            models.PointStruct(id=point_id, vector=[1.0, 0.0], payload={
                "character_id": owner, "kind": kind, "document_id": identifier,
                "revision": revision, "content_hash": "hash", "pool": "hot",
            })
            for point_id, owner, kind, identifier, revision in (
                (1, "c", "daily", "expired-daily", 2),
                (2, "c", "daily", "expired-daily", 2),
                (3, "c", "daily", "expired-daily", 1),
                (4, "c", "topic", "expired-topic", 2),
                (5, "c", "daily", "still-hot", 2),
                (6, "other", "daily", "expired-daily", 2),
            )
        ]
        await client.upsert(index.collection_name, points, wait=True)
        write_pool = client.set_payload

        async def fail_on_second(*args, **kwargs):
            if (await client.retrieve(index.collection_name, [1]))[0].payload["pool"] == "cold":
                raise RuntimeError("simulated index failure")
            return await write_pool(*args, **kwargs)

        monkeypatch.setattr(client, "set_payload", fail_on_second)
        service = MemoryRetentionService(SQLiteMemoryRetentionRepository(database), index)
        with pytest.raises(RuntimeError, match="simulated index failure"):
            await service.cool_expired_memories(today)
        async with database.db_session() as session:
            assert (await session.get(DailyMemoryRow, "expired-daily")).recall_tier == "hot"
            assert (await session.get(TopicMemoryRow, "expired-topic")).recall_tier == "hot"
        assert (await client.retrieve(index.collection_name, [1]))[0].payload["pool"] == "cold"
    finally:
        await client.close()

    client = AsyncQdrantClient(path=path)
    try:
        index = QdrantMemoryIndexRepository(client, None)
        service = MemoryRetentionService(SQLiteMemoryRetentionRepository(database), index)
        assert await service.cool_expired_memories(today) == 2
        assert await service.cool_expired_memories(today) == 0
        stored = await client.retrieve(index.collection_name, [1, 2, 3, 4, 5, 6])
        assert {point.id: point.payload["pool"] for point in stored} == {
            1: "cold", 2: "cold", 3: "hot", 4: "cold", 5: "hot", 6: "hot",
        }
        async with database.db_session() as session:
            for identifier, expected in (
                ("expired-daily", "cold"), ("still-hot", "hot"), ("cold", "cold"),
                ("archived", "archived"), ("missing-index", "hot"),
            ):
                assert (await session.get(DailyMemoryRow, identifier)).recall_tier == expected
            assert (await session.get(TopicMemoryRow, "expired-topic")).recall_tier == "cold"
    finally:
        await client.close()
