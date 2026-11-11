"""Topic首次登记：正式object来源、完整批次、幂等和事务回滚。"""
import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from module.shared.common.errors.base import InvalidStateError
from module.memory_management.shared.jobs.core.domain.command import CreateMemoryJob
from module.memory_management.shared.jobs.core.domain.job import MemoryJobStatus
from module.memory_management.object.core.domain.document import MemoryObject, ObjectEvent
from module.shared.memory.types.references import EvidenceRef
from module.memory_management.shared.repository import document_mappers
from module.memory_management.shared.jobs.repository import mappers as job_mappers
from module.memory_management.object.repository import mappers as object_mappers
from module.memory_management.topic.repository import mappers as topic_mappers
from module.chat.conversation.repository import mappers as conversation_mappers
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow, ConversationRow, SessionRow, MemoryJobRow, MemoryObjectRow
from module.memory_management.object.repository.sqlite import ObjectProductionRepository
from module.memory_management.topic.repository.sqlite import TopicProductionRepository
from module.memory_management.object.core.domain.job import ObjectJobInput
from module.memory_management.topic.core.domain.job import TopicJobInput
from module.memory_management.topic.repository.llm import TopicModelRepository


@pytest_asyncio.fixture
async def store(tmp_path):
    db = SQLiteDatabase(tmp_path / "topic.sqlite3")
    await db.initialize()
    now = datetime.now(timezone.utc)
    try:
        async with db.transaction() as session:
            session.add(CharacterRow(character_id="c", name="c", character_setting="test"))
            await session.flush()
            session.add(ConversationRow(conversation_id="conv", character_id="c", state="archived", created_at=now, updated_at=now))
            await session.flush()
            session.add_all(SessionRow(session_id=sid, conversation_id="conv", session_seq=1 if sid == "s" else 2, session_state="sealed",
                session_start_time=now, session_end_time=now) for sid in ("s", "s2"))
        yield db, TopicProductionRepository(db)
    finally:
        await db.close()


async def publish_objects(db, count=7, session_id="s"):
    repo = ObjectProductionRepository(db)
    jid = await repo.create_job(ObjectJobInput.create_command("c", session_id))
    job = await repo.get_job(jid)
    now = datetime.now(timezone.utc)
    objects = tuple(MemoryObject(f"obj{i}" if session_id == "s" else f"{session_id}_obj{i}", "c", f"项目{i}", (ObjectEvent("user", "项目已开始",
        date(2026, 11, 1), None, (EvidenceRef(session_id, "u", "原文"),)),), f"{session_id}:{i}", now) for i in range(count))
    async with db.transaction() as session:
        done = replace(job, status=MemoryJobStatus.DONE, objects=objects, finished_at=now)
        row = await session.get(MemoryJobRow, jid)
        row.status, row.payload_json = done.status.value, job_mappers.memory_job_payload_to_json(done)
        session.add_all(object_mappers.memory_object_to_row(item) for item in objects)
    return objects, jid


def batches(objects):
    return tuple(TopicJobInput.create_command("c", "s", tuple(item.object_id for item in objects[i:i+5]))
                 for i in range(0, len(objects), 5))


def topic_memory(topic_id, revision=1):
    from module.memory_management.topic.core.domain.document import TopicDocument, TopicIdentity, TopicCurrentState, TopicEntry
    from module.memory_management.shared.core.domain.document import TopicMemory
    from module.shared.memory.types.references import MemoryKind
    ref = EvidenceRef("s", "u", "证据原文")
    document = TopicDocument(topic_id, TopicIdentity("一件具体事件主题", (ref,)),
        TopicCurrentState("继续推进", ("event",)),
        (TopicEntry("event", "TIMELINE_ONLY", None, date(2026,11,1), (ref,)),))
    now = datetime.now(timezone.utc)
    return TopicMemory(document_id=topic_id, kind=MemoryKind.TOPIC, current_revision=revision,
        content_hash=document.content_hash(), vector_document_key=f"{topic_id}:{revision}",
        published_at=now, updated_at=now, source_refs=(ref,), document=document)


@pytest.mark.asyncio
async def test_read_topics_filters_publication_character_pool_and_preserves_order(store):
    from module.shared.memory.types.retention import RecallTier
    db, repo = store
    async with db.transaction() as session:
        session.add(CharacterRow(character_id="other", name="other", character_setting="test"))
        await session.flush()
        for topic_id, owner, pool in (("a", "c", RecallTier.HOT), ("b", "c", RecallTier.HOT),
                                      ("cold", "c", RecallTier.COLD), ("foreign", "other", RecallTier.HOT)):
            session.add(document_mappers.topic_memory_to_row(topic_memory(topic_id), character_id=owner, recall_tier=pool))
        pending = document_mappers.topic_memory_to_row(topic_memory("pending"), character_id="c")
        pending.published_at = None
        session.add(pending)
    result = await repo.read_topics(("b", "a", "a", "cold", "foreign", "missing", "pending"),
        character_id="c", pools=(RecallTier.HOT,))
    assert [item.ref.memory_id for item in result] == ["b", "a"]
    assert result[0].document == topic_memory("b").document
    assert await repo.read_topics((), character_id="c", pools=(RecallTier.HOT,)) == ()


@pytest.mark.asyncio
async def test_topic_recall_deduplicates_sections_and_navigation_before_rerank(store):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from module.memory_management.topic.core.processing.service import TopicProductionService
    from module.memory_management.topic.core.processing.config import TopicRecallConfig
    from module.memory_management.topic.core.domain.retrieval import TopicSearchGroup, TopicSearchHit, ObjectRecallGroup, ObjectRecallHit, TopicRerankScore
    db, repo = store
    objects, _ = await publish_objects(db, count=5)
    async with db.transaction() as session:
        for topic_id, revision in (("a", 2), ("b", 1), ("stale", 2)):
            session.add(document_mappers.topic_memory_to_row(topic_memory(topic_id, revision), character_id="c"))
        for object_id, topic_id in (("obj3", "a"), ("obj4", "b")):
            row = await session.get(MemoryObjectRow, object_id)
            row.object_state, row.linked_topic_id = "linked", topic_id
    direct = (
        TopicSearchGroup("obj0", (
            TopicSearchHit("obj0", "a", 2, "topic_name", "p1", 1, 0.02),
            TopicSearchHit("obj0", "a", 2, "topic_identity", "p2", 1, 0.03),
            TopicSearchHit("obj0", "a", 2, "topic_current", "p3", 1, 0.01),
            TopicSearchHit("obj0", "stale", 1, "topic_name", "old", 1, 0.04),
            TopicSearchHit("obj0", "missing", 1, "topic_name", "missing", 1, 0.05),
        )),
        TopicSearchGroup("obj1", (TopicSearchHit("obj1", "a", 2, "topic_name", "p1", 1, 0.02),)),
    )
    linked = tuple(ObjectRecallGroup(source, tuple(ObjectRecallHit(source, oid, i, 0.01, ("name",))
                    for i, oid in enumerate(("obj3", "obj4"), 1))) for source in ("obj0", "obj1"))
    recall = SimpleNamespace(batch_topic_search=AsyncMock(return_value=direct),
                             batch_linked_topic_search=AsyncMock(return_value=linked))
    reranker = SimpleNamespace(batch_topic_rerank=AsyncMock(return_value=(
        TopicRerankScore("obj0", "a", 0.2), TopicRerankScore("obj0", "b", 0.9),
        TopicRerankScore("obj1", "a", 0.6), TopicRerankScore("obj1", "b", 0.5))))
    service = TopicProductionService(repo, recall_repository=recall, rerank_repository=reranker,
                                    topic_recall_config=TopicRecallConfig(limit=1))
    result = await service.recall_related_topics(objects[:2])
    pairs = reranker.batch_topic_rerank.call_args.args[0]
    assert {(pair.source_object_id, pair.topic_id) for pair in pairs} == {
        ("obj0", "a"), ("obj0", "b"), ("obj1", "a"), ("obj1", "b")}
    assert len(pairs) == 4  # 三个分区＋导航不会重复打分，也不会被limit提前截断
    assert all("TIMELINE_ONLY" not in pair.document and "一件具体事件主题" in pair.document
               and "继续推进" in pair.document for pair in pairs)
    assert [[topic.ref.memory_id for topic in group.topics] for group in result] == [["b"], ["a"]]
    assert result[0].hits[0].linked and result[0].hits[0].rrf_score is None
    assert result[1].hits[0].sections == ("topic_name",) and result[1].hits[0].linked
    assert "TIMELINE_ONLY" in result[0].topics[0].document.entries[0].statement
    # 放宽数量上限后，仍在0.9→0.2处切刀；0.6→0.5不切。
    service = TopicProductionService(repo, recall_repository=recall, rerank_repository=reranker)
    result = await service.recall_related_topics(objects[:2])
    assert [[hit.topic_id for hit in group.hits] for group in result] == [["b"], ["a", "b"]]
    reranker.batch_topic_rerank.return_value = tuple(TopicRerankScore(source, topic, 0.1)
        for source in ("obj0", "obj1") for topic in ("a", "b"))
    assert all(group.topics == group.hits == () for group in await service.recall_related_topics(objects[:2]))
    assert await service.recall_related_topics(()) == ()


@pytest.mark.asyncio
async def test_scan_read_register_and_replay(store):
    db, repo = store
    assert await repo.get_unregister_sealed_sessions() == []
    objects, _ = await publish_objects(db)
    assert await repo.get_unregister_sealed_sessions() == ["s"]
    assert await repo.read_sealed_session_objects("s") == objects
    ids = await repo.register_topic_jobs_for_session("s", batches(objects))
    assert len(ids) == 2 and len(set(ids)) == 2
    assert await repo.register_topic_jobs_for_session("s", batches(objects)) == ()
    assert await repo.get_unregister_sealed_sessions() == []
    jobs = [await repo.get_job(jid) for jid in ids]
    assert all(job.status == MemoryJobStatus.NEED_GENERATE for job in jobs)
    assert jobs[0].target_key == jobs[1].target_key == "topic:c:consolidation"
    async with db.db_session() as session:
        row = await session.get(SessionRow, "s")
        assert row.topic_jobs_registered_at is not None and row.daily_jobs_registered_at is None
        domain = conversation_mappers.session_to_domain(row)
        assert conversation_mappers.session_to_row(domain).topic_jobs_registered_at == row.topic_jobs_registered_at
        assert all(row.object_state == "staged" for row in (await session.scalars(select(MemoryObjectRow))).all())
    command = TopicJobInput.create_command("c", "s", ("b", "a"))
    assert command.input.object_ids == ("b", "a")
    assert command.dedupe_key.startswith("topic:session:c:s:")
    assert command == TopicJobInput.create_command("c", "s", ("b", "a"))
    assert command.dedupe_key != TopicJobInput.create_command("c", "s", ("a", "b")).dedupe_key


@pytest.mark.asyncio
async def test_empty_objects_are_marked_complete(store):
    db, repo = store
    await publish_objects(db, count=0)
    assert await repo.get_unregister_sealed_sessions() == ["s"]
    assert await repo.register_topic_jobs_for_session("s", ()) == ()
    assert await repo.get_unregister_sealed_sessions() == []


@pytest.mark.asyncio
async def test_topic_begin_generate_atomic_without_loading_materials(store, monkeypatch):
    from unittest.mock import AsyncMock
    from module.memory_management.topic.core.domain.job import TopicJob
    db, repo = store
    objects, _ = await publish_objects(db, count=2)
    job_id = await repo.create_job(TopicJobInput.create_command("c", "s", (objects[0].object_id,)))
    load = AsyncMock(side_effect=AssertionError("开始生成不应读取模型材料"))
    monkeypatch.setattr(repo, "read_topic_source", load)
    results = await asyncio.gather(repo.begin_generate(job_id), repo.begin_generate(job_id))
    started = [job for job in results if job is not None]
    assert len(started) == 1 and isinstance(started[0], TopicJob)
    assert started[0].status == MemoryJobStatus.GENERATING
    assert started[0].attempt_version == started[0].attempt_count == 1
    assert started[0].input.object_ids == (objects[0].object_id,)
    assert started[0].plan is started[0].generation_result is started[0].source_manifest is None
    assert await repo.get_job(job_id) == started[0]
    assert await repo.begin_generate(job_id) is None
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_topic_begin_generate_respects_delayed_head(store):
    from datetime import timedelta
    db, repo = store
    objects, _ = await publish_objects(db, count=2)
    first = await repo.create_job(TopicJobInput.create_command("c", "s", (objects[0].object_id,)))
    second = await repo.create_job(TopicJobInput.create_command("c", "s", (objects[1].object_id,)))
    async with db.transaction() as session:
        head = await session.get(MemoryJobRow, first)
        head.available_at = datetime.now(timezone.utc) + timedelta(hours=1)
        tail = await session.get(MemoryJobRow, second)
        tail.created_at = head.created_at + timedelta(seconds=1)
    assert await repo.begin_generate(first) is None
    assert await repo.begin_generate(second) is None
    async with db.transaction() as session:
        head = await session.get(MemoryJobRow, first)
        head.status = MemoryJobStatus.DONE.value
    assert (await repo.begin_generate(second)).status == MemoryJobStatus.GENERATING


@pytest.mark.asyncio
async def test_topic_begin_generate_rejects_missing_or_other_family(store):
    from module.shared.common.errors.base import NotFoundError
    db, repo = store
    with pytest.raises(NotFoundError):
        await repo.begin_generate("missing")
    _, object_job_id = await publish_objects(db, count=1)
    with pytest.raises(InvalidStateError):
        await repo.begin_generate(object_job_id)


@pytest.mark.asyncio
async def test_generate_organizes_full_object_groups_and_saves_candidate(store, monkeypatch):
    from unittest.mock import AsyncMock
    from module.memory_management.topic.core.processing.service import TopicProductionService
    from module.memory_management.topic.core.domain.retrieval import RelatedObjectRecall, RelatedTopicRecall
    from module.memory_management.topic.core.domain.generation import TopicBatchResult
    from module.memory_management.topic.core.domain.source import TopicContext
    from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
    db, repo = store
    objects, _ = await publish_objects(db, count=3)
    history, _ = await publish_objects(db, count=1, session_id="s2")
    own_ids = tuple(item.object_id for item in objects[:2])
    job_id = await repo.create_job(TopicJobInput.create_command("c", "s", own_ids))
    job = await repo.get_job(job_id)
    memory = topic_memory("topic")
    context = TopicContext(MemoryRevisionRef(MemoryKind.TOPIC, "topic", memory.current_revision,
                                            memory.content_hash), memory.document)
    candidate = TopicBatchResult(1, (), own_ids, "hash", "prompt", datetime.now(timezone.utc))
    loaded = []
    async def generate(source, *, feedback=None):
        assert (await repo.get_job(job_id)).status == MemoryJobStatus.GENERATING
        loaded.append(source)
        return candidate
    service = TopicProductionService(repo, model=TopicModelRepository(object()))
    monkeypatch.setattr(service, "generate_topics", generate)
    object_recall = AsyncMock(return_value=(
        RelatedObjectRecall("obj0", (objects[2], history[0]), ()),
        RelatedObjectRecall("obj1", (history[0], objects[0]), ()),
    ))
    topic_recall = AsyncMock(return_value=(
        RelatedTopicRecall("obj0", (context,), ()), RelatedTopicRecall("obj1", (context,), ()),
    ))
    monkeypatch.setattr(service, "recall_related_objects", object_recall)
    monkeypatch.setattr(service, "recall_related_topics", topic_recall)
    read_source = AsyncMock(wraps=repo.read_sealed_session_source)
    monkeypatch.setattr(repo, "read_sealed_session_source", read_source)
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(repo, "save_generation_result", save)
    await service._generate(job)
    source = loaded[0]
    assert tuple(group.object for group in source.groups) == objects[:2]
    assert source.groups[0].related_objects == (objects[2], history[0])
    assert source.groups[1].related_objects == (history[0],)
    assert all(group.existing_topics == (context,) for group in source.groups)
    assert source.plan.object_ids == own_ids
    assert source.plan.related_object_ids == ("obj2", "s2_obj0")
    assert source.plan.topic_refs == (context.ref,)
    assert source.plan.min_distinct_sessions == 3
    read_source.assert_not_awaited()
    payload = source.to_model_payload()
    assert len(payload["GROUPS"]) == 2
    assert "RECALL_MATCHES" not in payload and "SOURCE_SESSIONS" not in payload
    assert "RELATED_CANDIDATES" not in payload and "EXISTING_TOPICS" not in payload
    for group in payload["GROUPS"]:
        assert group["RELATED_TOPICS"][0]["document"]["topic"] == "topic"
        assert group["RELATED_TOPICS"][0]["document"]["entries"][0]["statement"] == "TIMELINE_ONLY"
    assert [obj["object_id"] for obj in payload["GROUPS"][0]["RELATED_OBJECTS"]] == ["obj2", "s2_obj0"]
    assert [obj["object_id"] for obj in payload["GROUPS"][1]["RELATED_OBJECTS"]] == ["s2_obj0"]
    save.assert_awaited_once_with(job_id, 1, candidate, source=source)
    # 旧输入再次触发时，仓储不允许重复开始，后续召回和模型都不执行。
    await service._generate(job)
    assert len(loaded) == 1
    object_recall.assert_awaited_once_with(objects[:2])
    topic_recall.assert_awaited_once_with(objects[:2])


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "linked"])
async def test_generate_rejects_missing_or_consumed_new_objects(store, monkeypatch, case):
    from unittest.mock import AsyncMock
    from module.memory_management.topic.core.processing.service import TopicProductionService
    db, repo = store
    objects, _ = await publish_objects(db, count=1)
    job_id = await repo.create_job(TopicJobInput.create_command("c", "s", ("obj0",)))
    async with db.transaction() as session:
        row = await session.get(MemoryObjectRow, "obj0")
        if case == "missing":
            await session.delete(row)
        else:
            row.object_state, row.linked_topic_id = "linked", "previous-topic"
    generator = AsyncMock()
    service = TopicProductionService(repo, model=TopicModelRepository(object()))
    monkeypatch.setattr(service, "generate_topics", generator)
    save = AsyncMock()
    monkeypatch.setattr(repo, "save_generation_result", save)
    with pytest.raises(InvalidStateError):
        await service._generate(await repo.get_job(job_id))
    generator.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_without_model_does_not_start_job(store):
    from module.memory_management.topic.core.processing.service import TopicProductionService
    db, repo = store
    await publish_objects(db, count=1)
    job_id = await repo.create_job(TopicJobInput.create_command("c", "s", ("obj0",)))
    with pytest.raises(NotImplementedError, match="生成模型"):
        await TopicProductionService(repo)._generate(await repo.get_job(job_id))
    assert (await repo.get_job(job_id)).status == MemoryJobStatus.NEED_GENERATE


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "duplicate", "wrong_session", "wrong_key", "wrong_owner"])
async def test_bad_batches_do_not_mark_or_write(store, case):
    db, repo = store
    objects, _ = await publish_objects(db)
    commands = batches(objects)
    if case == "missing": commands = commands[:1]
    elif case == "duplicate": commands = (*commands, commands[0])
    elif case == "wrong_session": commands = (TopicJobInput.create_command("c", "s2", ("obj0",)),)
    elif case == "wrong_owner": commands = (TopicJobInput.create_command("other", "s", ("obj0",)),)
    else: commands = (replace(commands[0], dedupe_key="wrong"), commands[1])
    with pytest.raises(InvalidStateError):
        await repo.register_topic_jobs_for_session("s", commands)
    async with db.db_session() as session:
        assert (await session.get(SessionRow, "s")).topic_jobs_registered_at is None
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 1


@pytest.mark.asyncio
async def test_second_batch_failure_rolls_back_first_batch(store, monkeypatch):
    db, repo = store
    objects, _ = await publish_objects(db)
    original = repo.create_job_in_transaction
    calls = 0
    async def fail_second(session, command):
        nonlocal calls
        calls += 1
        if calls == 2: raise OSError("interrupted")
        return await original(session, command)
    with monkeypatch.context() as patch:
        patch.setattr(repo, "create_job_in_transaction", fail_second)
        with pytest.raises(OSError):
            await repo.register_topic_jobs_for_session("s", batches(objects))
    assert await repo.get_unregister_sealed_sessions() == ["s"]
    async with db.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 1
    assert len(await repo.register_topic_jobs_for_session("s", batches(objects))) == 2


@pytest.mark.asyncio
async def test_concurrent_registration_commits_one_complete_batch_set(store):
    db, repo = store
    objects, _ = await publish_objects(db)
    results = await asyncio.gather(*(repo.register_topic_jobs_for_session("s", batches(objects)) for _ in range(2)))
    assert sorted(len(result) for result in results) == [0, 2]
    async with db.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 3


@pytest.mark.asyncio
async def test_unfinished_extraction_and_missing_formal_object_are_rejected(store):
    db, repo = store
    objects, jid = await publish_objects(db)
    async with db.transaction() as session:
        (await session.get(MemoryJobRow, jid)).status = "need_activate"
    assert await repo.get_unregister_sealed_sessions() == []
    with pytest.raises(InvalidStateError):
        await repo.read_sealed_session_objects("s")
    async with db.transaction() as session:
        (await session.get(MemoryJobRow, jid)).status = "done"
        await session.delete(await session.get(MemoryObjectRow, objects[0].object_id))
    with pytest.raises(InvalidStateError):
        await repo.register_topic_jobs_for_session("s", batches(objects))


@pytest.mark.asyncio
async def test_single_batch_create_reuses_common_idempotency(store):
    db, repo = store
    objects, _ = await publish_objects(db)
    # 不依赖object_id字典序，登记和数据库往返均保留调用方的固定来源顺序。
    commands = tuple(TopicJobInput.create_command("c", "s", tuple(reversed(item.input.object_ids)))
                     for item in batches(objects))
    command = commands[0]
    first = await repo.create_job(command)
    assert await repo.create_job(command) == first
    assert (await repo.get_job(first)).input.object_ids == command.input.object_ids
    all_ids = await repo.register_topic_jobs_for_session("s", commands)
    assert all_ids[0] == first


@pytest.mark.asyncio
@pytest.mark.parametrize("count,batch_size,expected", [(0, 5, []), (1, 5, [1]), (5, 5, [5]), (12, 5, [5, 5, 2]), (7, 3, [3, 3, 1])])
async def test_service_first_registration_batches_in_source_order(store, monkeypatch, count, batch_size, expected):
    from module.memory_management.topic.core.processing.service import TopicProductionService
    from module.memory_management.topic.core.processing.config import TopicProductionConfig
    db, repo = store
    objects, _ = await publish_objects(db, count=count)
    service = TopicProductionService(repo, config=TopicProductionConfig(max_batch_objects=batch_size))
    async def forbidden(*args, **kwargs):
        raise AssertionError("首次登记不应读取聊天全文")
    monkeypatch.setattr(repo, "read_sealed_session_source", forbidden)
    ids = await service.register_topic_jobs(priority=7)
    jobs = [await repo.get_job(jid) for jid in ids]
    assert [len(job.input.object_ids) for job in jobs] == expected
    assert tuple(cid for job in jobs for cid in job.input.object_ids) == tuple(item.object_id for item in objects)
    assert all(job.priority == 7 and job.status == MemoryJobStatus.NEED_GENERATE for job in jobs)
    assert all(job.dedupe_key.startswith("topic:session:c:s:") for job in jobs)
    assert await service.register_topic_jobs(priority=7) == ()
    async with db.db_session() as session:
        assert (await session.get(SessionRow, "s")).topic_jobs_registered_at is not None


@pytest.mark.asyncio
async def test_service_failed_session_does_not_block_others_and_retries_next_scan(store, monkeypatch):
    from module.memory_management.topic.core.processing.service import TopicProductionService
    db, repo = store
    await publish_objects(db)
    await publish_objects(db, count=1, session_id="s2")
    service = TopicProductionService(repo)
    original = repo.register_topic_jobs_for_session
    async def fail_first(session_id, commands):
        if session_id == "s":
            raise OSError("temporary database failure")
        return await original(session_id, commands)
    with monkeypatch.context() as patch:
        patch.setattr(repo, "register_topic_jobs_for_session", fail_first)
        ids = await service.register_topic_jobs()
    assert len(ids) == 1
    assert (await repo.get_job(ids[0])).input.source_session_ids == ("s2",)
    assert await repo.get_unregister_sealed_sessions() == ["s"]
    assert len(await service.register_topic_jobs()) == 2
    assert await service.register_topic_jobs() == ()


@pytest.mark.asyncio
async def test_service_session_limit_is_independent_of_batch_size(store):
    from module.memory_management.topic.core.processing.service import TopicProductionService
    db, repo = store
    await publish_objects(db, count=7)
    await publish_objects(db, count=1, session_id="s2")
    service = TopicProductionService(repo)
    first = await service.register_topic_jobs(limit=1)
    assert len(first) == 2
    assert len(await repo.get_unregister_sealed_sessions()) == 1
    assert len(await service.register_topic_jobs(limit=1)) == 1


@pytest.mark.asyncio
async def test_service_recall_verifies_sqlite_without_refilling(store):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from module.memory_management.topic.core.processing.service import TopicProductionService
    from module.memory_management.topic.core.processing.config import ObjectRecallConfig
    from module.memory_management.topic.core.domain.retrieval import ObjectRecallHit, ObjectRecallGroup
    db, repo = store
    objects, _ = await publish_objects(db, count=5)
    async with db.transaction() as session:
        (await session.get(MemoryObjectRow, "obj1")).object_state = "discarded"
        linked = await session.get(MemoryObjectRow, "obj2")
        linked.object_state, linked.linked_topic_id = "linked", "topic-1"
        (await session.get(MemoryObjectRow, "obj3")).recall_tier = "cold"
    def group(ids, truncated):
        return (ObjectRecallGroup("obj0", tuple(ObjectRecallHit("obj0",cid,i+1,1/(i+1),("content",))
                for i,cid in enumerate(ids)), truncated),)
    from module.memory_management.topic.core.domain.retrieval import ObjectRerankScore
    reranker = SimpleNamespace(batch_object_rerank=AsyncMock(return_value=(ObjectRerankScore("obj0", "obj4", 0.8),)))
    raw = SimpleNamespace(batch_object_search=AsyncMock(return_value=group(("not-published", "obj1", "obj2", "obj3", "obj4"),True)))
    service=TopicProductionService(repo, recall_repository=raw, rerank_repository=reranker,
        recall_config=ObjectRecallConfig(limit=1, fetch_limit=5))
    results=await service.recall_related_objects((objects[0],))
    assert [item.object_id for item in results[0].pending_objects] == ["obj4"]
    assert {hit.object_id for hit in results[0].hits} == {"obj4"}
    assert results[0].truncated
    raw.batch_object_search.assert_awaited_once_with((objects[0],), config=service._retrieval._recall_config)
    # topic导航独立调用，忽略STAGED，不复用object召回的结果。
    raw.batch_linked_topic_search = AsyncMock(return_value=group(("obj4", "obj2", "obj2", "obj1"), True))
    topics = await service.recall_linked_topics((objects[0],))
    assert topics[0].linked_topic_ids == ("topic-1",)
    assert [hit.object_id for hit in topics[0].hits] == ["obj2"]
    raw.batch_linked_topic_search.assert_awaited_once()
    raw.batch_object_search.assert_awaited_once()
    assert await service.recall_linked_topics(()) == ()
    # 全部命中被过滤时仍只查询一次，保留截断标记。
    raw.batch_object_search.reset_mock()
    raw.batch_object_search.return_value=group(("not-published", "obj1", "obj3"),True)
    service=TopicProductionService(repo, recall_repository=raw, rerank_repository=reranker,
        recall_config=ObjectRecallConfig(limit=1, fetch_limit=3))
    result=(await service.recall_related_objects((objects[0],)))[0]
    assert result.pending_objects == result.hits == ()
    assert result.truncated
    raw.batch_object_search.assert_awaited_once()
    assert await service.recall_related_objects(()) == ()


@pytest.mark.asyncio
async def test_rerank_all_valid_objects_before_filtering_and_limit(store):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from module.memory_management.topic.core.processing.service import TopicProductionService
    from module.memory_management.topic.core.processing.config import ObjectRecallConfig
    from module.memory_management.topic.core.domain.retrieval import ObjectRecallHit, ObjectRecallGroup, ObjectRerankScore
    db, repo = store
    objects, _ = await publish_objects(db, count=5)
    group = ObjectRecallGroup("obj0", tuple(ObjectRecallHit("obj0", f"obj{i}", i, 1/i, ("content",))
                                           for i in range(1, 5)))
    recall = SimpleNamespace(batch_object_search=AsyncMock(return_value=(group,)))
    # 最初RRF第4名在rerank中最高；阈值只允许它通过。
    reranker = SimpleNamespace(batch_object_rerank=AsyncMock(return_value=tuple(
        ObjectRerankScore("obj0", f"obj{i}", score)
        for i, score in enumerate((0.1, 0.2, 0.3, 0.95), 1))))
    service = TopicProductionService(repo, recall_repository=recall, rerank_repository=reranker,
        recall_config=ObjectRecallConfig(limit=2, min_rerank_score=0.9))
    result = (await service.recall_related_objects((objects[0],)))[0]
    pairs = reranker.batch_object_rerank.call_args.args[0]
    assert len(pairs) == 4  # limit=2不能提前截掉第3、4名
    assert pairs[0].query == objects[0].index_text()
    assert pairs[0].document == objects[1].index_text()
    assert [item.object_id for item in result.pending_objects] == ["obj4"]
    assert result.hits[0].rank == 1
    assert result.hits[0].score == 0.25  # 保留原RRF分数
    assert result.hits[0].rerank_score == 0.95
    reranker.batch_object_rerank.return_value = tuple(ObjectRerankScore("obj0", f"obj{i}", 0.1) for i in range(1, 5))
    assert (await service.recall_related_objects((objects[0],)))[0].hits == ()


@pytest.mark.parametrize("scores, expected", [
    ([], []), ([0.14, 0.1], []), ([0.15], [0.15]),
    ([0.9, 0.2, 0.15], [0.9]),  # 后项腰斩且低于0.3，切第一刀
    ([0.9, 0.4, 0.19, 0.18], [0.9, 0.4]),
    ([0.6, 0.3], [0.6, 0.3]),  # 比例等于0.5，但0.3不能被护栏切掉
    ([0.4, 0.2, 0.19], [0.4]),  # 比例等于0.5应触发
    ([0.4, 0.21, 0.2], [0.4, 0.21, 0.2]),
    ([0.8, 0.7, 0.6], [0.8, 0.7, 0.6]),
])
def test_rerank_floor_and_first_cliff(scores, expected):
    from module.memory_management.topic.core.processing.service import TopicProductionService
    from module.memory_management.topic.core.domain.retrieval import ObjectRecallHit
    from module.memory_management.topic.core.processing.config import ObjectRecallConfig
    hits = [ObjectRecallHit("source", str(i), i + 1, 0.01, ("name",), score)
            for i, score in enumerate(scores)]
    selected = TopicProductionService._select_reranked_hits(list(reversed(hits)), ObjectRecallConfig())
    assert [hit.rerank_score for hit in selected] == expected
    assert [hit.rank for hit in selected] == list(range(1, len(expected) + 1))


@pytest.mark.asyncio
async def test_name_and_content_use_separate_rerank_pairs(store):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from module.memory_management.topic.core.processing.service import TopicProductionService
    from module.memory_management.topic.core.domain.retrieval import ObjectRecallHit, ObjectRecallGroup, ObjectRerankScore
    db, repo = store
    objects, _ = await publish_objects(db, count=4)
    group = ObjectRecallGroup("obj0", (
        ObjectRecallHit("obj0", "obj1", 1, 0.02, ("name",)),
        ObjectRecallHit("obj0", "obj2", 2, 0.01, ("content",)),
        ObjectRecallHit("obj0", "obj3", 3, 0.005, ("name", "content")),
    ))
    recall = SimpleNamespace(batch_object_search=AsyncMock(return_value=(group,)))
    reranker = SimpleNamespace(batch_object_rerank=AsyncMock(return_value=(
        ObjectRerankScore("obj0", "obj1", 0.2, "name"),
        ObjectRerankScore("obj0", "obj2", 0.6, "content"),
        ObjectRerankScore("obj0", "obj3", 0.8, "name"),
        ObjectRerankScore("obj0", "obj3", 0.1, "content"),
    )))
    service = TopicProductionService(repo, recall_repository=recall, rerank_repository=reranker)
    result = (await service.recall_related_objects((objects[0],)))[0]
    pairs = reranker.batch_object_rerank.call_args.args[0]
    assert [(pair.object_id, pair.section_key) for pair in pairs] == [
        ("obj1", "name"), ("obj2", "content"), ("obj3", "name"), ("obj3", "content")]
    assert pairs[0].query == objects[0].object
    assert pairs[0].document == objects[1].object
    assert pairs[1].query == objects[0].index_text()
    assert pairs[1].document == objects[2].index_text()
    assert [hit.object_id for hit in result.hits] == ["obj3", "obj2"]
    assert result.hits[0].name_rerank_score == 0.8
    assert result.hits[0].content_rerank_score == 0.1


@pytest.mark.parametrize("kwargs", [{"sparse_weight":-1}, {"dense_weight":float("nan")},
    {"cliff_ratio": 0}, {"cliff_ratio": 1}, {"cliff_score_guard": float("nan")},
    {"min_rerank_score": -0.1}, {"min_rerank_score": float("nan")},
    {"sparse_weight":0,"dense_weight":0}, {"limit":0}, {"fetch_limit":5}, {"pools":()}])
def test_object_recall_configuration_rejects_invalid_parameters(kwargs):
    from module.memory_management.topic.core.processing.config import ObjectRecallConfig
    with pytest.raises(ValueError):
        ObjectRecallConfig(**kwargs)


@pytest.mark.asyncio
async def test_save_topic_candidate_checkpoint_and_idempotency(store):
    from module.memory_management.topic.core.domain.source import TopicSourceInput, TopicObjectGroup
    from module.memory_management.topic.core.domain.plan import TopicPlan
    from module.memory_management.topic.core.domain.generation import TopicBatchResult
    db, repo = store
    objects, _ = await publish_objects(db, count=1)
    jid, = await repo.register_topic_jobs_for_session("s", batches(objects))
    started = await repo.begin_generate(jid)
    source = TopicSourceInput(TopicPlan("c", (objects[0].object_id,)), (TopicObjectGroup(objects[0]),))
    candidate = TopicBatchResult(1, (), source.plan.object_ids, "hash", "prompt", datetime.now(timezone.utc))
    assert not await repo.save_generation_result(jid, started.attempt_version - 1, candidate, source=source)
    assert await repo.save_generation_result(jid, started.attempt_version, candidate, source=source)
    saved = await repo.get_job(jid)
    assert saved.status == MemoryJobStatus.NEED_REVIEW
    assert saved.plan == source.plan and saved.generation_result == candidate and saved.generation_source == source
    assert await repo.save_generation_result(jid, started.attempt_version, candidate, source=source)
    assert not await repo.save_generation_result(jid, started.attempt_version, replace(candidate, content_hash="other"), source=source)


@pytest.mark.asyncio
async def test_save_topic_candidate_rejects_changed_objects(store):
    from module.memory_management.topic.core.domain.source import TopicSourceInput, TopicObjectGroup
    from module.memory_management.topic.core.domain.plan import TopicPlan
    from module.memory_management.topic.core.domain.generation import TopicBatchResult
    from module.memory_management.shared.contracts.errors import MemorySourceChangedError
    db, repo = store
    objects, _ = await publish_objects(db, count=1)
    jid, = await repo.register_topic_jobs_for_session("s", batches(objects))
    started = await repo.begin_generate(jid)
    source = TopicSourceInput(TopicPlan("c", (objects[0].object_id,)), (TopicObjectGroup(objects[0]),))
    candidate = TopicBatchResult(1, (), source.plan.object_ids, "hash", "prompt", datetime.now(timezone.utc))
    async with db.transaction() as session:
        row = await session.get(MemoryObjectRow, objects[0].object_id)
        await session.delete(row)
    with pytest.raises(MemorySourceChangedError):
        await repo.save_generation_result(jid, started.attempt_version, candidate, source=source)
    saved = await repo.get_job(jid)
    assert saved.status == MemoryJobStatus.GENERATING and saved.generation_result is None


@pytest.mark.asyncio
async def test_save_topic_candidate_rejects_changed_topic_base(store):
    from module.memory_management.topic.core.domain.source import TopicSourceInput, TopicObjectGroup
    from module.memory_management.topic.core.domain.plan import TopicPlan
    from module.memory_management.topic.core.domain.generation import TopicBatchResult
    from module.memory_management.shared.contracts.errors import MemorySourceChangedError
    from module.shared.memory.types.retention import RecallTier
    from storage.sqlite.models import TopicMemoryRow
    db, repo = store
    objects, _ = await publish_objects(db, count=1)
    async with db.transaction() as session:
        session.add(document_mappers.topic_memory_to_row(topic_memory("base"), character_id="c"))
    contexts = await repo.read_topics(("base",), character_id="c", pools=(RecallTier.HOT,))
    jid, = await repo.register_topic_jobs_for_session("s", batches(objects))
    started = await repo.begin_generate(jid)
    source = TopicSourceInput(TopicPlan("c", (objects[0].object_id,), topic_refs=(contexts[0].ref,)),
        (TopicObjectGroup(objects[0], existing_topics=contexts),))
    candidate = TopicBatchResult(1, (), source.plan.object_ids, "hash", "prompt", datetime.now(timezone.utc))
    assert topic_mappers.topic_source_from_json(topic_mappers.topic_source_to_json(source)) == source
    async with db.transaction() as session:
        row = await session.get(TopicMemoryRow, "base")
        row.revision += 1
    with pytest.raises(MemorySourceChangedError):
        await repo.save_generation_result(jid, started.attempt_version, candidate, source=source)
    assert (await repo.get_job(jid)).generation_result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("application_count", [0, 2])
async def test_topic_batch_review_passes_saved_applications(store, application_count):
    from unittest.mock import AsyncMock
    from module.memory_management.topic.core.processing.service import TopicProductionService
    from module.memory_management.topic.core.domain.source import TopicSourceInput, TopicObjectGroup
    from module.memory_management.topic.core.domain.plan import TopicPlan
    from module.memory_management.topic.core.domain.generation import TopicBatchResult, TopicApplication, TopicAction
    from module.memory_management.shared.contracts.review import ReviewVerdict
    db, repo = store
    objects, _ = await publish_objects(db, count=2)
    jid, = await repo.register_topic_jobs_for_session("s", batches(objects))
    started = await repo.begin_generate(jid)
    source = TopicSourceInput(TopicPlan("c", tuple(item.object_id for item in objects), min_distinct_sessions=1),
        tuple(TopicObjectGroup(item) for item in objects))
    applications = tuple(TopicApplication(f"a{i}", f"t{i}", TopicAction.CREATE,
        (objects[i].object_id,), topic_memory(f"t{i}").document, discussion_session_ids=("s",))
        for i in range(application_count))
    candidate = TopicBatchResult(1, applications, source.plan.object_ids if not applications else (),
        "hash", "prompt", datetime.now(timezone.utc))
    await repo.save_generation_result(jid, started.attempt_version, candidate, source=source)
    service = TopicProductionService(repo)
    service.review_topics = AsyncMock(wraps=service.review_topics)
    await service.process_job(jid)
    saved = await repo.get_job(jid)
    assert saved.status == MemoryJobStatus.NEED_WRITE
    service.review_topics.assert_awaited_once_with(candidate)
    assert [item.application_id for item in saved.review.application_reviews] == [app.application_id for app in applications]
    assert all(item.review_result == ReviewVerdict.PASS for item in saved.review.application_reviews)
    assert saved.review.accepted_applications(candidate) == applications
    assert saved.reviewed_version == candidate.result_version
    assert saved.reviewed_hash == candidate.content_hash
    assert saved.attempt_version == started.attempt_version + 1
    assert await repo.begin_review(jid) is None
    assert not await repo.save_review(jid, started.attempt_version, saved.review)
    assert await repo.save_review(jid, saved.attempt_version, saved.review)
    assert not await repo.save_review(jid, saved.attempt_version, replace(saved.review, result_hash="other"))


@pytest.mark.asyncio
async def test_topic_review_rejects_incomplete_batch(store):
    from module.memory_management.topic.core.domain.source import TopicSourceInput, TopicObjectGroup
    from module.memory_management.topic.core.domain.plan import TopicPlan
    from module.memory_management.topic.core.domain.generation import TopicBatchResult
    from module.memory_management.topic.core.domain.review import TopicReviewResult, TopicApplicationReview
    from module.memory_management.shared.contracts.review import ReviewVerdict
    db, repo = store
    objects, _ = await publish_objects(db, count=1)
    jid, = await repo.register_topic_jobs_for_session("s", batches(objects))
    started = await repo.begin_generate(jid)
    source = TopicSourceInput(TopicPlan("c", (objects[0].object_id,)), (TopicObjectGroup(objects[0]),))
    candidate = TopicBatchResult(1, (), source.plan.object_ids, "hash", "prompt", datetime.now(timezone.utc))
    await repo.save_generation_result(jid, started.attempt_version, candidate, source=source)
    reviewing = await repo.begin_review(jid)
    assert await repo.begin_review(jid) is None
    for review in (TopicReviewResult(1, "wrong", ()), TopicReviewResult(1, "hash",
            (TopicApplicationReview("unknown", ReviewVerdict.PASS),))):
        with pytest.raises(ValueError):
            await repo.save_review(jid, reviewing.attempt_version, review)
    assert (await repo.get_job(jid)).status == MemoryJobStatus.REVIEWING


async def prepare_topic_write_job(db, repo, verdict="pass"):
    from module.memory_management.topic.core.domain.source import TopicSourceInput, TopicObjectGroup
    from module.memory_management.topic.core.domain.plan import TopicPlan
    from module.memory_management.topic.core.domain.generation import TopicBatchResult, TopicApplication, TopicAction
    from module.memory_management.topic.core.domain.review import TopicReviewResult, TopicApplicationReview
    from module.memory_management.shared.contracts.review import ReviewVerdict
    from module.shared.memory.types.retention import RecallTier
    objects, _ = await publish_objects(db, count=2)
    async with db.transaction() as session:
        session.add(document_mappers.topic_memory_to_row(topic_memory("base", revision=3),
            character_id="c"))
    contexts = await repo.read_topics(("base",), character_id="c", pools=(RecallTier.HOT,))
    jid, = await repo.register_topic_jobs_for_session("s", batches(objects))
    started = await repo.begin_generate(jid)
    source = TopicSourceInput(TopicPlan("c", tuple(item.object_id for item in objects),
        topic_refs=(contexts[0].ref,), min_distinct_sessions=1),
        tuple(TopicObjectGroup(item, existing_topics=contexts) for item in objects))
    apps = () if verdict == "empty" else (
        TopicApplication("new-app", "new-topic", TopicAction.CREATE, (objects[0].object_id,),
            topic_memory("new-topic").document, discussion_session_ids=("s",)),
        TopicApplication("update-app", "base", TopicAction.UPDATE, (objects[1].object_id,),
            topic_memory("base").document, base_ref=contexts[0].ref))
    candidate = TopicBatchResult(1, apps, source.plan.object_ids if not apps else (),
        "hash", "prompt", datetime.now(timezone.utc))
    await repo.save_generation_result(jid, started.attempt_version, candidate, source=source)
    reviewing = await repo.begin_review(jid)
    reviews = tuple(TopicApplicationReview(app.application_id,
        ReviewVerdict.REVISED if verdict == "revised" and i == 1 else
        ReviewVerdict.REJECT if verdict in ("reject", "mixed") and (verdict == "reject" or i == 1) else ReviewVerdict.PASS,
        approved_document=replace(app.document, topic="审核修订后的主题") if verdict == "revised" and i == 1 else None)
        for i, app in enumerate(apps))
    await repo.save_review(jid, reviewing.attempt_version, TopicReviewResult(1, "hash", reviews))
    return jid


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["pass", "revised", "reject", "mixed", "empty"])
async def test_topic_prepare_publication_batch_roundtrip(store, verdict):
    from module.memory_management.topic.core.processing.service import TopicProductionService
    from module.memory_management.topic.core.domain.publication import TopicWriteItem
    from storage.sqlite.models import TopicMemoryRow
    db, repo = store
    jid = await prepare_topic_write_job(db, repo, verdict)
    before = await repo.get_job(jid)
    assert before.write_results is None
    await TopicProductionService(repo).process_job(jid)
    saved = await repo.get_job(jid)
    expected = tuple(TopicWriteItem.from_application(app) for app in saved.final_applications())
    assert saved.status == MemoryJobStatus.NEED_INDEX
    assert saved.write_results == expected
    assert saved.attempt_version == before.attempt_version + 1
    assert saved.attempt_count == before.attempt_count + 1
    assert await repo.begin_write(jid) is None
    assert await repo.save_write(jid, saved.attempt_version, expected)
    assert not await repo.save_write(jid, before.attempt_version, expected)
    if expected:
        assert expected[0].ref.memory_id == "new-topic" and expected[0].ref.revision == 1
        assert not await repo.save_write(jid, saved.attempt_version, ())
    if len(expected) == 2:
        assert expected[1].ref.memory_id == "base" and expected[1].ref.revision == 4
        if verdict == "revised":
            assert expected[1].write.document.topic == "审核修订后的主题"
    async with db.db_session() as session:
        assert await session.get(TopicMemoryRow, "new-topic") is None
        assert (await session.get(TopicMemoryRow, "base")).revision == 3
        rows = (await session.scalars(select(MemoryObjectRow))).all()
        assert all(object_mappers.memory_object_to_domain(row).state.value == "staged" for row in rows)


@pytest.mark.asyncio
async def test_topic_save_write_rejects_incomplete_or_changed_batch(store):
    from module.memory_management.topic.core.domain.publication import TopicWriteItem
    db, repo = store
    jid = await prepare_topic_write_job(db, repo)
    started = await repo.begin_write(jid)
    assert await repo.begin_write(jid) is None
    results = tuple(TopicWriteItem.from_application(app) for app in started.final_applications())
    bad_batches = [results[:1], results[::-1], (results[0], results[0]),
        (replace(results[0], application_id="other"), results[1]),
        (replace(results[0], ref=replace(results[0].ref, revision=2)), results[1]),
        (replace(results[0], write=replace(results[0].write, document=replace(results[0].write.document, topic="wrong"))), results[1])]
    for batch in bad_batches:
        with pytest.raises(InvalidStateError):
            await repo.save_write(jid, started.attempt_version, batch)
    saved = await repo.get_job(jid)
    assert saved.status == MemoryJobStatus.WRITING and saved.write_results is None
    assert await repo.save_write(jid, started.attempt_version, results)


@pytest.mark.asyncio
async def test_topic_begin_write_checks_review_binding_and_availability(store):
    from datetime import timedelta
    db, repo = store
    jid = await prepare_topic_write_job(db, repo)
    job = await repo.get_job(jid)
    async with db.transaction() as session:
        row = await session.get(MemoryJobRow, jid)
        future = replace(job, available_at=datetime.now(timezone.utc) + timedelta(hours=1))
        row.available_at = future.available_at
        row.payload_json = job_mappers.memory_job_payload_to_json(future)
    assert await repo.begin_write(jid) is None
    async with db.transaction() as session:
        row = await session.get(MemoryJobRow, jid)
        row.available_at = job.available_at
        row.payload_json = job_mappers.memory_job_payload_to_json(replace(job, reviewed_hash="stale"))
    with pytest.raises(InvalidStateError, match="审核未绑定"):
        await repo.begin_write(jid)
    assert (await repo.get_job(jid)).status == MemoryJobStatus.NEED_WRITE


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["pass", "empty"])
async def test_topic_index_batch_success_and_empty(store, verdict):
    from unittest.mock import AsyncMock
    from module.memory_management.topic.core.processing.service import TopicProductionService
    db, repo = store
    jid = await prepare_topic_write_job(db, repo, verdict)
    index = AsyncMock()
    index.batch_hybrid_index.return_value = True
    service = TopicProductionService(repo, index_repository=index if verdict == "pass" else None)
    await service.process_job(jid)
    before = await repo.get_job(jid)
    await service.process_job(jid)
    saved = await repo.get_job(jid)
    assert saved.status == MemoryJobStatus.NEED_ACTIVATE
    assert saved.write_results == before.write_results
    assert saved.attempt_version == before.attempt_version + 1
    assert await repo.begin_index(jid) is None
    assert not await repo.save_index(jid, before.attempt_version, saved.index_documents())
    assert await repo.save_index(jid, saved.attempt_version, saved.index_documents())
    if verdict == "pass":
        assert index.batch_hybrid_index.await_count == 2
        assert not await repo.save_index(jid, saved.attempt_version, ())
        for (document, representations), item in zip(
                (call.args for call in index.batch_hybrid_index.await_args_list), saved.write_results, strict=True):
            assert document.ref == item.ref and document.character_id == "c"
            assert document.document == item.write.document
            assert document.vector_document_key == f"{item.ref.memory_id}:r{item.ref.revision}:{item.ref.content_hash}"
            assert [rep.section_key for rep in representations] == ["topic_name", "topic_identity", "topic_current"]
            assert [rep.text for rep in representations] == [item.write.document.topic,
                item.write.document.topic + "\n" + item.write.document.topic_identity.description,
                item.write.document.topic + "\n" + item.write.document.current_state.text]
            assert all(rep.ref == item.ref and rep.recall_tier == saved.input.recall_tier for rep in representations)
    else:
        assert saved.write_results == ()
        index.batch_hybrid_index.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, "exception"])
async def test_topic_index_partial_failure_retries_stable_batch(store, failure):
    from unittest.mock import AsyncMock
    from module.memory_management.topic.core.processing.service import TopicProductionService
    from module.memory_management.shared.contracts.errors import MemoryIndexUnavailable
    db, repo = store
    jid = await prepare_topic_write_job(db, repo)
    index = AsyncMock()
    service = TopicProductionService(repo, index_repository=index)
    await service.process_job(jid)
    index.batch_hybrid_index.side_effect = [True, MemoryIndexUnavailable("temporary") if failure else False]
    with pytest.raises(MemoryIndexUnavailable):
        await service.process_job(jid)
    failed = await repo.get_job(jid)
    assert failed.status == MemoryJobStatus.INDEXING
    first_calls = list(index.batch_hybrid_index.await_args_list)
    assert await repo.recover_unfinished_attempts() == 1
    recovered = await repo.get_job(jid)
    assert recovered.status == MemoryJobStatus.NEED_INDEX
    assert recovered.write_results == failed.write_results
    index.batch_hybrid_index.reset_mock(side_effect=True)
    index.batch_hybrid_index.return_value = True
    await service.process_job(jid)
    assert index.batch_hybrid_index.await_args_list == first_calls
    assert (await repo.get_job(jid)).status == MemoryJobStatus.NEED_ACTIVATE
    assert not await repo.save_index(jid, failed.attempt_version, failed.index_documents())


@pytest.mark.asyncio
async def test_topic_index_claim_and_commit_guards(store):
    from module.memory_management.topic.core.processing.service import TopicProductionService
    db, repo = store
    jid = await prepare_topic_write_job(db, repo)
    service = TopicProductionService(repo)
    await service.process_job(jid)
    with pytest.raises(NotImplementedError):
        await service.process_job(jid)
    assert (await repo.get_job(jid)).status == MemoryJobStatus.NEED_INDEX
    started = await repo.begin_index(jid)
    assert await repo.begin_index(jid) is None
    documents = started.index_documents()
    for invalid in ((), documents[:1], documents[::-1],
                    (replace(documents[0], ref=replace(documents[0].ref, revision=99)), documents[1])):
        with pytest.raises(InvalidStateError):
            await repo.save_index(jid, started.attempt_version, invalid)
    assert (await repo.get_job(jid)).status == MemoryJobStatus.INDEXING
    assert await repo.save_index(jid, started.attempt_version, documents)


@pytest.mark.asyncio
async def test_topic_restart_generation_clears_publication_checkpoint(store):
    from module.memory_management.topic.core.processing.service import TopicProductionService
    db, repo = store
    jid = await prepare_topic_write_job(db, repo)
    await TopicProductionService(repo).process_job(jid)
    job = await repo.get_job(jid)
    assert await repo.retry_job(jid, job.attempt_version, expected_status=job.status,
        available_at=datetime.now(timezone.utc), error="source changed", restart_generation=True)
    restarted = await repo.get_job(jid)
    assert restarted.status == MemoryJobStatus.NEED_GENERATE
    assert restarted.plan is None and restarted.generation_source is None
    assert restarted.generation_result is None and restarted.review is None and restarted.write_results is None


async def prepare_topic_activation(db, repo, verdict="pass"):
    from unittest.mock import AsyncMock
    from module.memory_management.topic.core.processing.service import TopicProductionService
    jid = await prepare_topic_write_job(db, repo, verdict)
    index = AsyncMock()
    index.batch_hybrid_index.return_value = True
    service = TopicProductionService(repo, index_repository=index)
    await service.process_job(jid)
    await service.process_job(jid)
    return jid, service


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["pass", "revised", "mixed", "reject", "empty"])
async def test_topic_activation_publishes_batch_and_links_only_adopted_objects(store, verdict, tmp_path):
    from pathlib import Path
    import hashlib
    from storage.sqlite.models import TopicMemoryRow
    from module.memory_management.object.core.domain.document import ObjectState
    db, repo = store
    jid, service = await prepare_topic_activation(db, repo, verdict)
    before = await repo.get_job(jid)
    assert not before.publication_started
    prior_mention = date(2026, 9, 1)
    async with db.transaction() as session:
        base = await session.get(TopicMemoryRow, "base")
        if base is not None:
            base.last_mentioned_at = prior_mention
    assert not await repo.activate_revision(jid, before.attempt_version - 1)
    await service.process_job(jid)
    saved = await repo.get_job(jid)
    assert saved.status == MemoryJobStatus.DONE and saved.publication_started
    assert not list(tmp_path.rglob("*.md"))
    assert saved.published_refs == tuple(item.ref for item in before.write_results)
    adopted = {cid: app.topic_id for app in saved.final_applications() for cid in app.object_ids}
    async with db.db_session() as session:
        for item in saved.write_results:
            row = await session.get(TopicMemoryRow, item.ref.memory_id)
            memory = document_mappers.topic_memory_to_domain(row)
            assert memory.document == item.write.document
            assert (memory.current_revision, memory.content_hash) == (item.ref.revision, item.ref.content_hash)
            assert row.character_id == "c" and row.recall_tier == saved.input.recall_tier.value
            expected_mention = (
                prior_mention if row.topic_id == "base"
                else row.published_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
            )
            assert row.last_mentioned_at == expected_mention
            assert memory.vector_document_key == f"{item.ref.memory_id}:r{item.ref.revision}:{item.ref.content_hash}"
        rows = (await session.scalars(select(MemoryObjectRow))).all()
        for row in rows:
            assert row.linked_topic_id == adopted.get(row.object_id)
            assert row.object_state == (ObjectState.LINKED.value if row.object_id in adopted else ObjectState.STAGED.value)
    assert await repo.activate_revision(jid, saved.attempt_version)
    assert await repo.get_job(jid) == saved


@pytest.mark.asyncio
async def test_topic_activation_final_transaction_failure_recovers_same_revision(store):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from storage.sqlite.models import TopicMemoryRow
    db, repo = store
    jid, service = await prepare_topic_activation(db, repo)
    async with db.transaction() as session:
        await session.execute(text("CREATE TRIGGER reject_done BEFORE UPDATE ON memory_jobs WHEN NEW.status='done' BEGIN SELECT RAISE(ABORT, 'test failure'); END;"))
    with pytest.raises(IntegrityError):
        await service.process_job(jid)
    pending = await repo.get_job(jid)
    assert pending.status == MemoryJobStatus.NEED_ACTIVATE and pending.publication_started
    async with db.transaction() as session:
        await session.execute(text("DROP TRIGGER reject_done"))
    await service.process_job(jid)
    done = await repo.get_job(jid)
    assert done.status == MemoryJobStatus.DONE and done.published_refs == pending.published_refs
    async with db.db_session() as session:
        assert (await session.get(TopicMemoryRow, 'new-topic')).revision == 1
        assert (await session.get(TopicMemoryRow, 'base')).revision == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["base", "object", "foreign"])
async def test_topic_activation_conflict_rolls_back_whole_batch(store, conflict):
    from storage.sqlite.models import TopicMemoryRow
    from module.memory_management.shared.contracts.errors import MemorySourceChangedError
    db, repo = store
    jid, service = await prepare_topic_activation(db, repo)
    async with db.transaction() as session:
        if conflict == "base":
            (await session.get(TopicMemoryRow, "base")).revision += 1
        elif conflict == "object":
            obj = await session.get(MemoryObjectRow, "obj1")
            obj.object_state, obj.linked_topic_id = "linked", "other"
        else:
            session.add(CharacterRow(character_id="foreign", name="foreign", character_setting="test"))
            await session.flush()
            session.add(document_mappers.topic_memory_to_row(topic_memory("new-topic"), character_id="foreign"))
    with pytest.raises(MemorySourceChangedError):
        await service.process_job(jid)
    job = await repo.get_job(jid)
    assert job.status == MemoryJobStatus.NEED_ACTIVATE and not job.publication_started
    async with db.db_session() as session:
        assert (await session.get(MemoryObjectRow, "obj0")).object_state == "staged"
        assert (await session.get(TopicMemoryRow, "base")).revision == (4 if conflict == "base" else 3)
        if conflict != "foreign":
            assert await session.get(TopicMemoryRow, "new-topic") is None


@pytest.mark.asyncio
async def test_topic_activation_retry_cannot_overwrite_newer_version(store, monkeypatch):
    from storage.sqlite.models import TopicMemoryRow
    from module.memory_management.shared.contracts.errors import MemorySourceChangedError
    db, repo = store
    jid, service = await prepare_topic_activation(db, repo)
    job = await repo.get_job(jid)
    await repo.prepare_activation(jid, job.attempt_version)
    async with db.transaction() as session:
        (await session.get(TopicMemoryRow, "base")).revision += 1
    with pytest.raises(MemorySourceChangedError):
        await service.process_job(jid)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, "exception"])
async def test_topic_metadata_sync_failure_keeps_activation_retryable(store, failure):
    from unittest.mock import AsyncMock
    from module.memory_management.shared.contracts.errors import MemoryIndexUnavailable
    from storage.sqlite.models import TopicMemoryRow
    db, repo = store
    jid, service = await prepare_topic_activation(db, repo)
    index = service._index_repository
    index.update_object_metadata.side_effect = MemoryIndexUnavailable("temporary") if failure else None
    index.update_object_metadata.return_value = False
    with pytest.raises(MemoryIndexUnavailable):
        await service.process_job(jid)
    interrupted = await repo.get_job(jid)
    assert interrupted.status == MemoryJobStatus.NEED_ACTIVATE and interrupted.publication_started
    candidates, = index.update_object_metadata.await_args.args
    assert [(item.object_id, item.state.value, item.linked_topic_id) for item in candidates] == [
        ("obj0", "linked", "new-topic"), ("obj1", "linked", "base")]
    # 模拟同一NEED_ACTIVATE阶段退避后重试，正式版本保持不变。
    assert await repo.retry_job(jid, interrupted.attempt_version, expected_status=interrupted.status,
        available_at=datetime.now(timezone.utc), error="temporary")
    index.update_object_metadata.side_effect = None
    index.update_object_metadata.return_value = True
    await service.process_job(jid)
    completed = await repo.get_job(jid)
    assert completed.status == MemoryJobStatus.DONE and completed.published_refs == interrupted.published_refs
    assert index.update_object_metadata.await_count == 2
    async with db.db_session() as session:
        assert (await session.get(TopicMemoryRow, "new-topic")).revision == 1
        assert (await session.get(TopicMemoryRow, "base")).revision == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["mixed", "reject", "empty"])
async def test_topic_metadata_sync_only_accepted_objects(store, verdict):
    db, repo = store
    jid, service = await prepare_topic_activation(db, repo, verdict)
    index = service._index_repository
    await service.process_job(jid)
    if verdict == "mixed":
        candidates, = index.update_object_metadata.await_args.args
        assert [(item.object_id, item.linked_topic_id) for item in candidates] == [("obj0", "new-topic")]
    else:
        index.update_object_metadata.assert_not_awaited()
    assert (await repo.get_job(jid)).status == MemoryJobStatus.DONE


@pytest.mark.asyncio
async def test_topic_activation_updates_real_qdrant_payload_without_encoding(store, tmp_path):
    import numpy as np
    from qdrant_client import AsyncQdrantClient
    from module.memory_management.shared.indexing.repository.qdrant import QdrantMemoryIndexRepository
    from module.memory_management.topic.repository.retrieval import QdrantMemoryRecallRepository
    from module.memory_management.topic.core.processing.config import ObjectRecallConfig
    class Encoder:
        def __init__(self):
            self.calls = 0
        def encode(self, texts, **kwargs):
            self.calls += 1
            return {"dense_vecs": np.ones((len(texts), 1024), dtype=np.float32),
                "lexical_weights": [{"1": 1.0} for _ in texts]}
    db, repo = store
    jid, service = await prepare_topic_activation(db, repo)
    staged = await repo.read_objects(("obj0", "obj1"), character_id="c")
    client = AsyncQdrantClient(path=str(tmp_path / "qdrant-sync"))
    encoder = Encoder()
    index = QdrantMemoryIndexRepository(client, encoder)
    try:
        assert await index.batch_object_index(staged)
        points, _ = await client.scroll(index.object_collection_name, limit=10, with_payload=True, with_vectors=True)
        original_vectors = {point.id: point.vector for point in points}
        original_calls = encoder.calls
        service._index_repository = index
        await service.process_job(jid)
        assert encoder.calls == original_calls
        assert (await repo.get_job(jid)).status == MemoryJobStatus.DONE
        points, _ = await client.scroll(index.object_collection_name, limit=10, with_payload=True, with_vectors=True)
        assert len(points) == 4
        for point in points:
            assert point.vector == original_vectors[point.id]
            assert point.payload["state"] == "linked"
            assert point.payload["linked_topic_id"] == ("new-topic" if point.payload["object_id"] == "obj0" else "base")
        recall = QdrantMemoryRecallRepository(client, encoder, index_repository=index)
        query = replace(staged[0], object_id="query")
        groups = await recall.batch_linked_topic_search((query,), config=ObjectRecallConfig())
        assert {hit.object_id for hit in groups[0].hits} == {"obj0", "obj1"}
    finally:
        await client.close()
