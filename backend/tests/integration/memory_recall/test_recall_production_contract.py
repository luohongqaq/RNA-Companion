"""真实SQLite与本地Qdrant验证production→recall，编码器不调用外部服务。"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace
import json
import numpy as np
import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient
from sqlalchemy import text
from module.shared.common.errors.base import InvalidStateError
from module.memory_management.daily.core.domain.document import DailyDocument, DailyMoment, DailyReflection, MomentKind
from module.memory_management.daily.core.domain.publication import DailyWrite
from module.memory_management.topic.core.domain.document import TopicDocument, TopicIdentity, TopicCurrentState, TopicEntry
from module.memory_management.topic.core.domain.publication import TopicWrite
from module.shared.memory.types.references import EvidenceRef, MemoryKind, MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier
from module.memory_management.shared.core.domain.document import DailyMemory, TopicMemory
from module.memory_management.shared.core.domain.document import VectorDocument
from module.memory_recall.retrieval.core.processing.session import MemoryReadSession
from module.memory_recall.retrieval.core.domain.document_snapshot import RecallDocumentSnapshot
from module.shared.common.time import BackendTime
from module.memory_recall.cache.core.processing.service import MemoryCacheService
from module.memory_recall.retrieval.core.domain.tools.search import SearchInput
from module.memory_recall.retrieval.core.domain.tools.read import ReadInput
from module.memory_recall.retrieval.core.domain.tools.listing import ListInput
from module.memory_recall.retrieval.core.domain.tools.common import TimeRange
from module.memory_management.shared.repository import document_mappers
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow, ConversationRow, DailyMemoryRow
from module.memory_recall.cache.repository.sqlite import SQLiteRecallCache
from module.memory_management.shared.indexing.repository.qdrant import QdrantMemoryIndexRepository
from module.memory_management.daily.repository.qdrant import DailyIndexRepository
from module.memory_recall.retrieval.repository.reader_factory import RecallReaderFactory
from api.cli.reindex_memories import reindex_character


def test_recall_date_range_uses_backend_midnights_across_dst(monkeypatch):
    local = BackendTime.local
    zone = ZoneInfo("America/New_York")
    monkeypatch.setattr(BackendTime, "local", staticmethod(lambda value: local(value, zone=zone)))
    start, end = RecallDocumentSnapshot._day_interval("2026-03-08")
    assert start == datetime(2026, 3, 8, 5, tzinfo=timezone.utc)
    assert end - start == timedelta(hours=23)
    document = {"record_dates": ["2026-03-08"], "kind": "daily", "sections": {"notes": "text"}}
    snapshot = RecallDocumentSnapshot((SimpleNamespace(to_recall_payload=lambda: document),))
    window = TimeRange(basis="conversation_time", start=start, end=end)
    assert snapshot.filter_by_time(window, "exclude", pool=None, kinds=("daily",))[0] == [document]
    next_window = TimeRange(basis="conversation_time", start=end, end=end + timedelta(days=1))
    assert snapshot.filter_by_time(next_window, "exclude", pool=None, kinds=("daily",))[0] == []


class MaterialCounter:
    def count_text(self, text):
        return len(text)


class Encoder:
    def encode(self, texts, **kwargs):
        return {"dense_vecs": np.ones((len(texts), 1024), dtype=np.float32),
                "lexical_weights": [{"1": 1.0} for _ in texts]}


class Store:
    def __init__(self, db, client):
        self.db, self.client = db, client
        self.encoder = Encoder()
        self.readers = RecallReaderFactory(client, self.encoder, database=db, token_counter=MaterialCounter())
        self.index = QdrantMemoryIndexRepository(client, self.encoder)

    def reader(self, character="c"):
        return self.readers.create(character)

    async def publish(self, document, identifier, revision=1, owner="c", pool=RecallTier.HOT, activate=True):
        kind = MemoryKind.DAILY if isinstance(document, DailyDocument) else MemoryKind.TOPIC
        ref = MemoryRevisionRef(kind, identifier, revision, document.content_hash())
        key = f"{identifier}:r{revision}:{ref.content_hash}"
        vector = VectorDocument(key, ref, owner, document, (), (), document.title if kind == MemoryKind.DAILY else document.topic)
        write = (DailyWrite if kind == MemoryKind.DAILY else TopicWrite).from_document(document)
        index = DailyIndexRepository.from_shared(self.index) if kind == MemoryKind.DAILY else self.index
        assert await index.batch_hybrid_index(vector, write.index_representations(ref, owner, pool))
        if activate:
            now = datetime.now(timezone.utc)
            common = dict(document_id=identifier, kind=kind, current_revision=revision,
                content_hash=ref.content_hash, vector_document_key=key, published_at=now, updated_at=now, document=document)
            if kind == MemoryKind.DAILY:
                memory = DailyMemory(**common, memory_date=document.memory_date, source_session_ids=document.source_session_ids)
                row = document_mappers.daily_memory_to_row(memory, character_id=owner, recall_tier=pool)
            else:
                memory = TopicMemory(**common, source_refs=(),)
                row = document_mappers.topic_memory_to_row(memory, character_id=owner, recall_tier=pool)
            async with self.db.transaction() as session:
                await session.merge(row)
        return ref


@pytest_asyncio.fixture
async def store(tmp_path):
    db = SQLiteDatabase(tmp_path / "recall.sqlite3")
    client = AsyncQdrantClient(path=str(tmp_path / "qdrant"))
    await db.initialize()
    now = datetime.now(timezone.utc)
    async with db.transaction() as session:
        session.add_all(CharacterRow(character_id=cid, name=cid, character_setting="test") for cid in ("c", "other"))
        await session.flush()
        session.add(ConversationRow(conversation_id="conv", character_id="c", state="active", created_at=now, updated_at=now))
    try:
        yield Store(db, client)
    finally:
        await client.close()
        await db.close()


def daily(day=1, title="项目学习"):
    return DailyDocument(date(2026, 11, day), ("s",), title,
        memorable_moments=(DailyMoment(MomentKind.MILESTONE, "项目有了进展", ("t",)),),
        reflections=(DailyReflection("我希望继续陪伴", ("t",)),))


def topic():
    proof = EvidenceRef("s", "u", "INTERNAL_EVIDENCE_DO_NOT_EXPOSE")
    return TopicDocument("项目上线", TopicIdentity("一个具体项目", (proof,)), TopicCurrentState("继续推进", ("new",)), (
        TopicEntry("old", "此前记录错误", date(2026, 10, 1), date(2026, 11, 1), (proof,)),
        TopicEntry("new", "已更正为今天上线", date(2026, 11, 2), date(2026, 11, 2), (proof,), corrects_entry_id="old"),
        TopicEntry("plan", "计划下周继续", None, date(2026, 11, 2), (proof,), kind="plan", target_time=date(2026, 11, 9)),
        TopicEntry("unknown", "某次进展时间不详", None, date(2026, 11, 2), (proof,))))


@pytest.mark.asyncio
async def test_daily_index_rejects_topic_before_writing_qdrant(store):
    document = topic()
    ref = MemoryRevisionRef(MemoryKind.TOPIC, "topic", 1, document.content_hash())
    vector = VectorDocument("topic:r1", ref, "c", document, (), (), document.topic)
    representations = TopicWrite.from_document(document).index_representations(ref, "c", RecallTier.HOT)
    index = DailyIndexRepository.from_shared(store.index)
    with pytest.raises(ValueError, match="只接受 daily"):
        await index.batch_hybrid_index(vector, representations)
    assert not await store.client.collection_exists(store.index.collection_name)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["grep", "semantic", "hybrid"])
async def test_production_sections_search_read_and_dedup(store, mode):
    await store.publish(daily(), "daily")
    await store.publish(topic(), "topic")
    session = MemoryReadSession(store.reader())
    result = await session.search(SearchInput(mode=mode, purpose="find", literal_terms=["项目"] if mode != "semantic" else [],
        query="项目进展" if mode != "grep" else None))
    assert result.status == "ok" and len(result.items) == 2
    by_kind = {item.kind: item for item in result.items}
    assert set(by_kind["daily"].sections) == {"memorable_moments", "reflections"}
    assert set(by_kind["topic"].sections) == {"topic_identity", "current_state"}
    assert by_kind["daily"].time_context.memory_date == "2026-11-01"
    assert by_kind["daily"].time_context.conversation_time is None
    read = await session.read(ReadInput(purpose="details", card_handles=[by_kind["topic"].card_handle]))
    assert read.status == "ok" and "已更正" in read.reads[0].sections["entries"]
    assert "INTERNAL_EVIDENCE" not in json.dumps(read.model_dump(mode="json"))
    assert "entries" in session.materialize([dict(read_handle=read.reads[0].read_handle, section_keys=["entries"])])[0]["sections"]


@pytest.mark.asyncio
async def test_only_current_published_version_and_attempt_pins_full_text(store):
    old = daily(title="项目旧版")
    new = replace(old, title="项目新版")
    old_ref = await store.publish(old, "daily")
    await store.publish(new, "daily", revision=2, activate=False)
    session = MemoryReadSession(store.reader())
    result = await session.search(SearchInput(mode="semantic", purpose="find", query="项目"))
    assert len(result.items) == 1 and result.items[0].title == old.title
    handle = result.items[0].card_handle
    await store.publish(new, "daily", revision=2)
    pinned = await session.read(ReadInput(purpose="read", card_handles=[handle]))
    assert pinned.reads[0].title == old.title
    fresh = await store.reader().load_records(["daily"])
    assert fresh["daily"]["revision"] == 2 and fresh["daily"]["content_hash"] != old_ref.content_hash
    result = await MemoryReadSession(store.reader()).search(SearchInput(mode="semantic", purpose="find", query="项目"))
    assert len(result.items) == 1 and result.items[0].title == new.title


@pytest.mark.asyncio
async def test_scope_pools_and_catalog_are_formal_sqlite_documents(store):
    await store.publish(daily(), "hot")
    await store.publish(daily(2), "cold", pool=RecallTier.COLD)
    await store.publish(daily(3), "archived", pool=RecallTier.ARCHIVED)
    await store.publish(daily(), "foreign", owner="other")
    await store.publish(daily(4), "unpublished", activate=False)
    session = MemoryReadSession(store.reader())
    search = await session.search(SearchInput(mode="semantic", purpose="find", query="项目"))
    assert [session.cards[item.card_handle] for item in search.items] == ["hot"]
    listed = await session.list_memories(ListInput(mode="overview", purpose="list"))
    assert {session.cards[item.card_handle] for item in listed.items} == {"hot"}
    assert not listed.coverage.includes_hot_and_cold
    fallback = await session.list_memories(ListInput(mode="fallback", purpose="find older memory",
        history_clue="用户提起之前交流过的项目", keywords=["项目"]))
    assert {session.cards[item.card_handle] for item in fallback.items} == {"cold"}
    assert not fallback.coverage.includes_hot_and_cold
    assert (await session.temporal_coverage())["document_count"] == 2
    assert set(await store.reader().load_records(["hot", "cold", "foreign", "archived", "unpublished"])) == {"hot", "cold"}


@pytest.mark.asyncio
async def test_date_filters_do_not_treat_plans_or_corrected_events_as_occurred(store):
    await store.publish(topic(), "topic")
    async def listed(day, basis="event_time", unknown="exclude"):
        session = MemoryReadSession(store.reader())
        return await session.list_memories(ListInput(mode="overview", purpose="date", unknown_event_time=unknown,
            time_range=TimeRange(basis=basis, start=f"2026-{day}T12:00:00+08:00", end=f"2026-{day}T13:00:00+08:00")))
    assert (await listed("11-02")).items
    assert not (await listed("10-01")).items
    assert not (await listed("11-09")).items
    assert (await listed("11-09", unknown="include")).items
    heard = await listed("11-01", basis="conversation_time")
    assert heard.items and heard.items[0].time_context.time_precision == "date"
    assert heard.items[0].time_context.plan_dates[0]["target_time"] == "2026-11-09"


@pytest.mark.asyncio
async def test_legacy_point_metadata_requires_reindex_without_revision_change(store):
    ref = await store.publish(daily(), "daily")
    points, _ = await store.client.scroll(store.index.collection_name, limit=20)
    for point in points:
        await store.client.delete_payload(store.index.collection_name, keys=["document_id", "content_hash"], points=[point.id], wait=True)
    result = await MemoryReadSession(store.reader()).search(SearchInput(mode="semantic", purpose="find", query="项目"))
    assert result.items == []
    assert await reindex_character(store.db, store.index, "c") == 1
    result = await MemoryReadSession(store.reader()).search(SearchInput(mode="semantic", purpose="find", query="项目"))
    assert len(result.items) == 1
    assert (await store.reader().load_records(["daily"]))["daily"]["content_hash"] == ref.content_hash


@pytest.mark.asyncio
async def test_legacy_cache_is_refreshed_and_archived_records_are_removed(store):
    await store.publish(daily(), "daily")
    async with store.db.transaction() as session:
        await session.execute(text("INSERT INTO memory_recall_cache(character_id,conversation_id,version,signal_seq,payload_json) VALUES ('c','conv',1,0,:payload)"),
            {"payload": json.dumps({"recent_update_memory": [], "recent_retrievals": [{"document_id":"daily", "sections":{"episode_summary":"obsolete"}}]})})
    cache_service = MemoryCacheService(SQLiteRecallCache(store.db))
    cache = await cache_service.prepare("c", "conv", store.reader())
    assert cache.refresh_complete and cache.recent_update_memory
    assert "memorable_moments" in cache.recent_update_memory[0]["sections"]
    assert "episode_summary" not in str(cache.recent_update_memory)
    async with store.db.transaction() as session:
        (await session.get(DailyMemoryRow, "daily")).recall_tier = "archived"
    refreshed = await cache_service.prepare("c", "conv", store.reader())
    assert refreshed.refresh_complete and refreshed.recent_update_memory == []


@pytest.mark.asyncio
async def test_cache_reuses_documents_across_pool_changes_and_refreshes_new_revision(store):
    document = daily()
    await store.publish(document, "hot")
    await store.publish(daily(2), "cold", pool=RecallTier.COLD)
    cache_service = MemoryCacheService(SQLiteRecallCache(store.db))
    reader = store.reader()
    cache = await cache_service.prepare("c", "conv", reader)
    session = MemoryReadSession(reader, cache)
    await session.search(SearchInput(mode="grep", purpose="find", literal_terms=["项目"]))
    await session.list_memories(ListInput(mode="fallback", purpose="find older memory",
        history_clue="用户提起之前交流过的项目"))
    await cache_service.remember("c", "conv", cache, session.recent_retrievals())
    assert {row["document_id"] for row in cache.recent_retrievals} == {"hot", "cold"}

    async with store.db.transaction() as db:
        (await db.get(DailyMemoryRow, "hot")).recall_tier = "cold"
    reused = await cache_service.prepare("c", "conv", store.reader())
    assert reused.signal_seq == cache.signal_seq
    assert reused.recent_retrievals == cache.recent_retrievals
    assert reused.recent_update_memory == cache.recent_update_memory

    new = replace(document, title="项目新版")
    await store.publish(new, "hot", revision=2, pool=RecallTier.COLD)
    refreshed = await cache_service.prepare("c", "conv", store.reader())
    records = {row["document_id"]: row for row in refreshed.recent_retrievals}
    assert records["hot"]["revision"] == 2 and records["hot"]["title"] == new.title
    assert records["cold"]["revision"] == 1


@pytest.mark.asyncio
async def test_empty_catalog_and_corrupt_formal_hash(store):
    assert (await MemoryReadSession(store.reader()).list_memories(ListInput(mode="overview", purpose="empty"))).items == []
    await store.publish(daily(), "daily")
    async with store.db.transaction() as session:
        (await session.get(DailyMemoryRow, "daily")).content_hash = "corrupt"
    with pytest.raises(InvalidStateError, match="hash"):
        await store.reader().load_records(["daily"])


@pytest.mark.asyncio
async def test_qdrant_preview_cannot_replace_sqlite_body(store):
    await store.publish(daily(), "daily")
    points, _ = await store.client.scroll(store.index.collection_name, limit=20)
    await store.client.set_payload(store.index.collection_name,
        payload={"memorable_moments": "FORGED_PREVIEW", "title": "FORGED_TITLE"},
        points=[point.id for point in points], wait=True)
    result = await MemoryReadSession(store.reader()).search(SearchInput(mode="semantic", purpose="check", query="项目"))
    assert len(result.items) == 1 and result.items[0].title == daily().title
    assert "FORGED" not in str(result.model_dump())


@pytest.mark.asyncio
async def test_seed_import_uses_production_hash_sections_and_formal_topic(store, tmp_path):
    from storage.sqlite.seed_memories import SQLiteSeedMemoryRepository
    repository = SQLiteSeedMemoryRepository(store.db)
    d, t = daily(), topic()
    documents = [dict(document_id="seed-daily", kind="daily", title=d.title, memory_date=d.memory_date.isoformat(),
        conversation_time="2026-11-01T12:00:00+08:00", sections={key: value for key, value in d.to_data().items()
            if key not in ("title", "memory_date", "source_session_ids")}),
        dict(document_id="seed-topic", kind="topic", title=t.topic, sections={key: value for key, value in t.to_data().items() if key != "topic"})]
    kwargs = dict(character_id="c", namespace="test", encoder=store.encoder)
    first = await store.index.import_documents(store.client, repository, documents, **kwargs)
    again = await store.index.import_documents(store.client, repository, documents, **kwargs)
    assert first["created"] == 2 and again["unchanged"] == 2
    records = await store.reader().load_records(["seed-daily", "seed-topic"])
    assert len(records) == 2 and "entries" in records["seed-topic"]["sections"]
    result = await MemoryReadSession(store.reader()).search(SearchInput(mode="semantic", purpose="seed", query="项目"))
    assert len(result.items) == 2


@pytest.mark.asyncio
async def test_cache_does_not_reuse_corrupt_content_even_with_same_version(store):
    await store.publish(daily(), "daily")
    cache = MemoryCacheService(SQLiteRecallCache(store.db))
    await cache.prepare("c", "conv", store.reader())
    async with store.db.transaction() as session:
        row = await session.get(DailyMemoryRow, "daily")
        row.document_json = dict(row.document_json, title="tampered-without-new-hash")
    with pytest.raises(InvalidStateError, match="hash"):
        await cache.prepare("c", "conv", store.reader())
