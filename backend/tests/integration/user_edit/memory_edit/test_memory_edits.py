"""真实SQLite验证编辑原子保存、版本并发、固定快照、索引重试及重启恢复。"""
import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock
from types import SimpleNamespace
import httpx

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from module.shared.common.errors.base import IdempotencyConflictError, InvalidInputError, InvalidStateError, NotFoundError
from module.shared.jobs.config.retry import RetryPolicy
from module.user_edit.memory_edit.core.scheduling.workflow import MemoryEditWorkflow
from module.user_edit.memory_edit.core.scheduling.worker import MemoryEditWorker
from module.user_edit.memory_edit.core.processing.query_service import MemoryQueryService
from module.user_edit.memory_edit.core.processing.edit_service import MemoryEditService
from module.user_edit.memory_edit.core.processing.index_service import MemoryEditIndexService
from module.memory_management.shared.contracts.errors import MemoryIndexUnavailable
from module.memory_management.daily.core.domain.document import DailyDocument, DailyMoment, MomentKind
from module.memory_management.topic.core.domain.document import TopicCurrentState, TopicDocument, TopicEntry, TopicIdentity
from module.memory_management.shared.core.domain.document import DailyMemory, TopicMemory
from module.user_edit.memory_edit.core.domain.job import MemoryEditStatus
from module.user_edit.memory_edit.core.domain.command import UpdateDailyMemory, UpdateTopicMemory
from module.user_edit.memory_edit.core.domain.document import DailyMomentContent
from module.shared.memory.types.references import EvidenceRef, MemoryKind
from module.shared.memory.types.retention import RecallTier
from module.memory_management.shared.repository import document_mappers
from storage.sqlite.db_session import SQLiteDatabase
from module.user_edit.memory_edit.repository.sqlite import SQLiteMemoryEditRepository
from storage.sqlite.models import CharacterRow, DailyMemoryRow, MemoryEditRow, TopicMemoryRow
from api.app import create_app

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def case(tmp_path):
    db = SQLiteDatabase(tmp_path / "edits.db")
    await db.initialize()
    daily = DailyDocument(NOW.date(), ("sealed-session",), "日记",
        (DailyMoment(MomentKind.MILESTONE, "旧记录", ("turn-1",)),))
    refs = (EvidenceRef("session-1", "message-1", "原话"),)
    topic = TopicDocument("主题", TopicIdentity("说明", refs), TopicCurrentState("进度", ("entry-1",)),
        (TopicEntry("entry-1", "旧条目", None, NOW.date(), refs),))
    async with db.transaction() as session:
        session.add(CharacterRow(character_id="c", name="角色", character_setting="test"))
        await session.flush()
        session.add(document_mappers.daily_memory_to_row(DailyMemory(document_id="d", kind=MemoryKind.DAILY,
            current_revision=1, content_hash=daily.content_hash(), vector_document_key="old-d", published_at=NOW,
            updated_at=NOW, memory_date=NOW.date(), source_session_ids=daily.source_session_ids, document=daily),
            character_id="c", recall_tier=RecallTier.HOT))
        session.add(document_mappers.topic_memory_to_row(TopicMemory(document_id="t", kind=MemoryKind.TOPIC,
            current_revision=1, content_hash=topic.content_hash(), vector_document_key="old-t", published_at=NOW,
            updated_at=NOW, source_refs=refs, document=topic), character_id="c", recall_tier=RecallTier.HOT))
    repository = SQLiteMemoryEditRepository(db)
    index = AsyncMock()
    index.batch_hybrid_index.return_value = True
    service = SimpleNamespace(query=MemoryQueryService(repository), edit=MemoryEditService(repository),
        index=MemoryEditIndexService(repository, index_repository=index))
    try:
        yield db, repository, service, index
    finally:
        await db.close()


async def command(service, kind="daily", **changes):
    document_id = "d" if kind == "daily" else "t"
    current = await service.query.get_memory(character_id="c", kind=MemoryKind(kind), document_id=document_id)
    cls = UpdateDailyMemory if kind == "daily" else UpdateTopicMemory
    return cls(**(dict(character_id="c", document_id=document_id, expected_revision=current.revision,
                       idempotency_key="request-1", document=current.document) | changes))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["daily", "topic"])
async def test_save_is_immediately_visible_and_index_is_separate(case, kind):
    db, repository, service, index = case
    cmd = await command(service, kind)
    if kind == "daily":
        body = replace(cmd.document, title="  用户标题  ", memorable_moments=(
            DailyMomentContent(MomentKind.RELATIONSHIP, "  用户正文  "),))
    else:
        body = replace(cmd.document, topic="用户主题", entries=(replace(cmd.document.entries[0], statement="用户内容"),))
    save = getattr(service.edit, f"update_{kind}_memory")
    result = await save(replace(cmd, document=body))
    assert result.revision == 2 and result.index_status == MemoryEditStatus.PENDING
    index.batch_hybrid_index.assert_not_called()
    detail = await service.query.get_memory(character_id="c", kind=MemoryKind(kind), document_id=cmd.document_id)
    assert detail.document == body
    async with db.db_session() as session:
        row = await session.get(MemoryEditRow, result.edit_id)
        assert (row.base_revision, row.revision) == (1, 2)
        assert row.content_hash != row.base_content_hash
        if kind == "daily":
            assert row.document_json["source_session_ids"] == ["sealed-session"]
            assert row.document_json["memorable_moments"][0]["source_turn_ids"] == []
        else:
            assert row.document_json["entries"][0]["source_refs"] == []
    edit = await repository.claim_next_memory_edit()
    await service.index.index_memory_edit(edit)
    vector, representations = index.batch_hybrid_index.call_args.args
    assert vector.ref.revision == 2 and vector.ref.content_hash == edit.content_hash
    assert all(item.ref == vector.ref for item in representations)
    async with db.db_session() as session:
        assert (await session.get(MemoryEditRow, edit.edit_id)).index_status == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["daily", "topic"])
async def test_unchanged_entries_keep_sources_and_idempotent_replay_survives_later_edit(case, kind):
    db, _, service, _ = case
    save = getattr(service.edit, f"update_{kind}_memory")
    cmd = await command(service, kind)
    first = await save(cmd)
    later = await command(service, kind, idempotency_key="request-2")
    await save(later)
    replay = await save(cmd)
    assert replay.edit_id == first.edit_id and replay.revision == 2
    async with db.db_session() as session:
        row = await session.get(MemoryEditRow, first.edit_id)
        if kind == "daily":
            assert row.document_json["memorable_moments"][0]["source_turn_ids"] == ["turn-1"]
        else:
            assert row.document_json["entries"][0]["source_refs"]
        assert await session.scalar(select(func.count()).select_from(MemoryEditRow)) == 2


@pytest.mark.asyncio
async def test_key_collision_and_stale_revision_are_rejected(case):
    _, _, service, _ = case
    cmd = await command(service)
    await service.edit.update_daily_memory(cmd)
    with pytest.raises(IdempotencyConflictError):
        await service.edit.update_daily_memory(replace(cmd, document=replace(cmd.document, title="不同内容")))
    with pytest.raises(InvalidStateError, match="版本"):
        await service.edit.update_daily_memory(replace(cmd, idempotency_key="other-request"))


@pytest.mark.asyncio
async def test_concurrent_saves_only_one_version_wins(case):
    db, _, service, _ = case
    cmd = await command(service)
    results = await asyncio.gather(service.edit.update_daily_memory(cmd),
        service.edit.update_daily_memory(replace(cmd, idempotency_key="request-2")), return_exceptions=True)
    assert sum(isinstance(result, InvalidStateError) for result in results) == 1
    async with db.db_session() as session:
        assert (await session.get(DailyMemoryRow, "d")).revision == 2
        assert await session.scalar(select(func.count()).select_from(MemoryEditRow)) == 1


@pytest.mark.asyncio
async def test_failed_edit_insert_rolls_back_formal_document(case):
    db, _, service, _ = case
    cmd = await command(service)
    async with db.transaction() as session:
        await session.execute(text("CREATE TRIGGER reject_edit BEFORE INSERT ON memory_edits "
                                   "BEGIN SELECT RAISE(ABORT, 'test insert failure'); END;"))
    with pytest.raises(Exception, match="test insert failure"):
        await service.edit.update_daily_memory(cmd)
    async with db.db_session() as session:
        assert (await session.get(DailyMemoryRow, "d")).revision == 1
        assert await session.scalar(select(func.count()).select_from(MemoryEditRow)) == 0


@pytest.mark.asyncio
async def test_empty_daily_and_topic_timeline_are_allowed(case):
    _, repository, service, index = case
    daily = await command(service)
    await service.edit.update_daily_memory(replace(daily, document=replace(daily.document, memorable_moments=())))
    topic = await command(service, "topic", idempotency_key="topic-request")
    await service.edit.update_topic_memory(replace(topic, document=replace(topic.document, entries=(),
        current_state=TopicCurrentState("", ()))))
    edit = await repository.claim_next_memory_edit()
    await service.index.index_memory_edit(edit)
    assert index.batch_hybrid_index.call_args.args[1] == ()
    detail = await service.query.get_memory(character_id="c", kind=MemoryKind.TOPIC, document_id="t")
    assert detail.document.entries == ()


@pytest.mark.asyncio
async def test_invalid_date_reference_and_foreign_role_rejected(case):
    _, _, service, _ = case
    daily = await command(service)
    with pytest.raises(InvalidInputError):
        await service.edit.update_daily_memory(replace(daily, document=replace(daily.document, memory_date=date(2020, 1, 1))))
    with pytest.raises(NotFoundError):
        await service.edit.update_daily_memory(replace(daily, character_id="foreign"))
    topic = await command(service, "topic")
    with pytest.raises(InvalidInputError):
        await service.edit.update_topic_memory(replace(topic, document=replace(topic.document, entries=())))
    with pytest.raises(InvalidInputError):
        await service.edit.update_topic_memory(replace(topic, document=replace(topic.document,
            entries=topic.document.entries * 2)))


@pytest.mark.asyncio
async def test_new_version_skips_old_pending_index(case):
    db, repository, service, _ = case
    first = await service.edit.update_daily_memory(await command(service))
    second = await service.edit.update_daily_memory(await command(service, idempotency_key="request-2"))
    edit = await repository.claim_next_memory_edit()
    assert edit.edit_id == second.edit_id
    assert await repository.claim_next_memory_edit() is None
    async with db.db_session() as session:
        assert (await session.get(MemoryEditRow, first.edit_id)).index_status == "obsolete"


@pytest.mark.asyncio
async def test_late_index_completion_does_not_overwrite_new_edit(case):
    db, repository, service, _ = case
    first = await service.edit.update_daily_memory(await command(service))
    old = await repository.claim_next_memory_edit()
    await service.edit.update_daily_memory(await command(service, idempotency_key="request-2"))
    await service.index.index_memory_edit(old)
    async with db.db_session() as session:
        assert (await session.get(MemoryEditRow, first.edit_id)).index_status == "obsolete"
        assert (await session.get(DailyMemoryRow, "d")).revision == 3


@pytest.mark.asyncio
async def test_restart_invalidates_late_attempt_but_preserves_snapshot(case):
    _, repository, service, _ = case
    await service.edit.update_daily_memory(await command(service))
    old = await repository.claim_next_memory_edit()
    assert await repository.recover_memory_edit_indexes() == 1
    new = await repository.claim_next_memory_edit()
    assert new.attempt_version > old.attempt_version and new.document == old.document
    assert not await repository.complete_memory_edit_index(old.edit_id, old.attempt_version)
    assert not await repository.retry_memory_edit_index(old.edit_id, old.attempt_version, available_at=NOW, error="late")
    await service.index.index_memory_edit(new)


@pytest.mark.asyncio
async def test_retry_and_terminal_failure_preserve_saved_content(case):
    db, repository, service, index = case
    saved = await service.edit.update_daily_memory(await command(service))
    index.batch_hybrid_index.side_effect = MemoryIndexUnavailable("temporary")
    policy = RetryPolicy(max_retries=1, delays=(timedelta(0),))
    workflow = MemoryEditWorkflow(repository, service.index, retry_policy=policy)
    first = await repository.claim_next_memory_edit()
    assert await workflow.execute(first)
    second = await repository.claim_next_memory_edit()
    assert second.retry_count == 1
    assert await workflow.execute(second)
    async with db.db_session() as session:
        row = await session.get(MemoryEditRow, saved.edit_id)
        assert row.index_status == "failed" and row.last_error == "temporary"
        assert (await session.get(DailyMemoryRow, "d")).revision == 2


@pytest.mark.asyncio
async def test_worker_restarts_and_executes_persisted_edit(case):
    db, repository, service, index = case
    saved = await service.edit.update_daily_memory(await command(service))
    await repository.claim_next_memory_edit()  # 模拟旧进程在编码前退出
    worker = MemoryEditWorker(repository, MemoryEditWorkflow(repository, service.index), poll_interval=0.01)
    await worker.start()
    try:
        async def wait_done():
            while True:
                async with db.db_session() as session:
                    if (await session.get(MemoryEditRow, saved.edit_id)).index_status == "done":
                        return
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_done(), timeout=3)
        index.batch_hybrid_index.assert_awaited_once()
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_transient_failure_then_success_reuses_the_same_version(case):
    db, repository, service, index = case
    saved = await service.edit.update_daily_memory(await command(service))
    index.batch_hybrid_index.side_effect = [MemoryIndexUnavailable("temporary"), True]
    workflow = MemoryEditWorkflow(repository, service.index,
        retry_policy=RetryPolicy(max_retries=1, delays=(timedelta(0),)))
    await workflow.execute(await repository.claim_next_memory_edit())
    await workflow.execute(await repository.claim_next_memory_edit())
    calls = index.batch_hybrid_index.call_args_list
    assert calls[0].args == calls[1].args
    async with db.db_session() as session:
        assert (await session.get(MemoryEditRow, saved.edit_id)).index_status == "done"


@pytest.mark.asyncio
async def test_non_retryable_index_error_ends_only_index_work(case):
    db, repository, service, index = case
    saved = await service.edit.update_daily_memory(await command(service))
    index.batch_hybrid_index.side_effect = MemoryIndexUnavailable("invalid vector", retryable=False)
    await MemoryEditWorkflow(repository, service.index).execute(await repository.claim_next_memory_edit())
    assert await repository.claim_next_memory_edit() is None
    async with db.db_session() as session:
        row = await session.get(MemoryEditRow, saved.edit_id)
        assert (row.index_status, row.retry_count) == ("failed", 0)
        assert (await session.get(DailyMemoryRow, "d")).revision == 2


@pytest.mark.asyncio
async def test_worker_retries_when_completion_persistence_itself_fails(case, monkeypatch):
    db, repository, service, _ = case
    saved = await service.edit.update_daily_memory(await command(service))
    original = repository.complete_memory_edit_index
    monkeypatch.setattr(repository, "complete_memory_edit_index", AsyncMock(side_effect=OSError("db unavailable")))
    monkeypatch.setattr(repository, "retry_memory_edit_index", AsyncMock(side_effect=OSError("db unavailable")))
    worker = MemoryEditWorker(repository, MemoryEditWorkflow(repository, service.index))
    with pytest.raises(OSError):
        await worker.process_next()
    monkeypatch.setattr(repository, "complete_memory_edit_index", original)
    assert await worker.process_next()
    async with db.db_session() as session:
        assert (await session.get(MemoryEditRow, saved.edit_id)).index_status == "done"


@pytest.mark.asyncio
async def test_local_qdrant_write_is_idempotent_for_the_same_snapshot(case, monkeypatch):
    from qdrant_client import AsyncQdrantClient, models
    from module.memory_management.shared.indexing.repository.qdrant import QdrantMemoryIndexRepository
    from storage.qdrant.memory_schema import COLLECTION, DIM

    _, repository, service, _ = case
    client = AsyncQdrantClient(location=":memory:")
    index = QdrantMemoryIndexRepository(client, encoder=None)
    monkeypatch.setattr(index, "_encode", lambda texts: [
        {"dense": [1.0] + [0.0] * (DIM - 1), "sparse": models.SparseVector(indices=[1], values=[1.0])}
        for _ in texts])
    service.index.configure_index(index)
    service.edit.set_index_notifier(lambda: None)
    try:
        saved = await service.edit.update_daily_memory(await command(service))
        edit = await repository.claim_next_memory_edit()
        await service.index.index_memory_edit(edit)
        first, _ = await client.scroll(COLLECTION, limit=100, with_payload=True)
        assert len(first) == 1 and first[0].payload["revision"] == saved.revision
        assert first[0].payload["content_hash"] == edit.content_hash
        await service.index.index_memory_edit(edit)
        repeated, _ = await client.scroll(COLLECTION, limit=100, with_payload=True)
        assert [point.id for point in repeated] == [point.id for point in first]
    finally:
        await client.close()


@pytest_asyncio.fixture
async def edit_client(case):
    _, _, service, _ = case
    app = create_app()
    app.state.container = SimpleNamespace(memory_query_service=service.query, memory_edit_service=service.edit)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("index_status", ["pending", "indexing", "done", "failed", "obsolete"])
async def test_edit_status_query_and_retry_reject_non_failed_states(case, edit_client, index_status):
    db, _, service, _ = case
    saved = await service.edit.update_daily_memory(await command(service))
    async with db.transaction() as session:
        row = await session.get(MemoryEditRow, saved.edit_id)
        row.index_status = index_status
        row.indexed_at = NOW if index_status == "done" else None
        row.last_error = "编码失败" if index_status == "failed" else None
    path = f"/api/v1/memory-edits/{saved.edit_id}"
    response = await edit_client.get(path)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["index_status"] == index_status and data["revision"] == saved.revision
    assert data["character_id"] == "c" and data["retry_count"] == 0
    assert set(data) == {"edit_id", "document_id", "kind", "revision", "index_status", "character_id",
                         "retry_count", "available_at", "error", "indexed_at", "created_at", "updated_at"}
    assert "source_turn_ids" not in response.text and "document_json" not in response.text
    if index_status != "failed":
        assert (await edit_client.post(path + "/retry")).status_code == 409


@pytest.mark.asyncio
async def test_http_manual_retry_requeues_without_changing_saved_document(case, edit_client):
    db, repository, service, index = case
    saved = await service.edit.update_daily_memory(await command(service))
    old = await repository.claim_next_memory_edit()
    await repository.fail_memory_edit_index(old.edit_id, old.attempt_version, error="temporary")
    async with db.transaction() as session:
        row = await session.get(MemoryEditRow, saved.edit_id)
        row.retry_count = 3
    response = await edit_client.post(f"/api/v1/memory-edits/{saved.edit_id}/retry")
    assert response.status_code == 202, response.text
    data = response.json()
    assert data["index_status"] == "pending" and data["retry_count"] == 0 and data["error"] is None
    index.batch_hybrid_index.assert_not_awaited()
    async with db.db_session() as session:
        row = await session.get(MemoryEditRow, saved.edit_id)
        assert row.attempt_version > old.attempt_version
        assert (await session.get(DailyMemoryRow, "d")).revision == 2
        assert await session.scalar(select(func.count()).select_from(MemoryEditRow)) == 1
    assert not await repository.complete_memory_edit_index(old.edit_id, old.attempt_version)
    assert await MemoryEditWorker(repository, MemoryEditWorkflow(repository, service.index)).process_next()
    completed = await edit_client.get(f"/api/v1/memory-edits/{saved.edit_id}")
    assert completed.json()["index_status"] == "done" and completed.json()["indexed_at"] is not None


@pytest.mark.asyncio
async def test_http_retry_obsolete_failure_is_rejected_and_marks_obsolete(case, edit_client):
    db, repository, service, _ = case
    saved = await service.edit.update_daily_memory(await command(service))
    old = await repository.claim_next_memory_edit()
    await repository.fail_memory_edit_index(old.edit_id, old.attempt_version, error="temporary")
    await service.edit.update_daily_memory(await command(service, idempotency_key="new-request"))
    response = await edit_client.post(f"/api/v1/memory-edits/{saved.edit_id}/retry")
    assert response.status_code == 409, response.text
    result = await edit_client.get(f"/api/v1/memory-edits/{saved.edit_id}")
    assert result.json()["index_status"] == "obsolete"
    async with db.db_session() as session:
        assert (await session.get(DailyMemoryRow, "d")).revision == 3


@pytest.mark.asyncio
async def test_concurrent_manual_retries_only_requeue_once(case, edit_client):
    _, repository, service, _ = case
    saved = await service.edit.update_daily_memory(await command(service))
    edit = await repository.claim_next_memory_edit()
    await repository.fail_memory_edit_index(edit.edit_id, edit.attempt_version, error="temporary")
    path = f"/api/v1/memory-edits/{saved.edit_id}/retry"
    results = await asyncio.gather(edit_client.post(path), edit_client.post(path))
    assert sorted(response.status_code for response in results) == [202, 409]


@pytest.mark.asyncio
async def test_edit_query_and_retry_missing_id_and_notification_failure(case, edit_client):
    _, repository, service, index = case
    assert (await edit_client.get("/api/v1/memory-edits/missing")).status_code == 404
    assert (await edit_client.post("/api/v1/memory-edits/missing/retry")).status_code == 404
    saved = await service.edit.update_daily_memory(await command(service))
    edit = await repository.claim_next_memory_edit()
    await repository.fail_memory_edit_index(edit.edit_id, edit.attempt_version, error="temporary")
    def fail_notify():
        raise RuntimeError("notification unavailable")
    service.index.configure_index(index)
    service.edit.set_index_notifier(fail_notify)
    assert (await edit_client.post(f"/api/v1/memory-edits/{saved.edit_id}/retry")).status_code == 202
    assert (await service.edit.get_memory_edit(saved.edit_id)).index_status == MemoryEditStatus.PENDING
