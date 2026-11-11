"""管理仓储的正式记忆查询，以及服务与消息列表一致的游标流程。"""
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import base64
import json

import pytest
import pytest_asyncio
from sqlalchemy import delete
from types import SimpleNamespace
import httpx
from api.app import create_app

from module.shared.common.errors.base import InvalidInputError, InvalidStateError, NotFoundError
from module.shared.common.types.pagination import Page
from module.user_edit.memory_edit.core.processing.query_service import MemoryQueryService
from module.user_edit.memory_edit.core.processing.edit_service import MemoryEditService
from module.memory_management.daily.core.domain.document import DailyDocument, DailyMoment, MomentKind
from module.memory_management.topic.core.domain.document import TopicCurrentState, TopicDocument, TopicEntry, TopicIdentity
from module.memory_management.shared.core.domain.document import DailyMemory, TopicMemory
from module.shared.memory.types.references import EvidenceRef, MemoryKind
from module.shared.memory.types.retention import RecallTier
from module.memory_management.shared.repository import document_mappers
from storage.sqlite.db_session import SQLiteDatabase
from module.user_edit.memory_edit.repository.sqlite import SQLiteMemoryEditRepository
from storage.sqlite.models import CharacterRow, DailyMemoryRow, TopicMemoryRow

NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def memory_row(kind, identifier, *, owner="c", day=4, updated_at=NOW, tier=RecallTier.HOT):
    if kind == MemoryKind.DAILY:
        document = DailyDocument(date(2026, 11, day), (), f"日记-{identifier}",
            (DailyMoment(MomentKind.MILESTONE, "记录", ("private-turn",)),))
        memory = DailyMemory(document_id=identifier, kind=kind, current_revision=2,
            content_hash=document.content_hash(), vector_document_key="vector", published_at=NOW,
            updated_at=updated_at, memory_date=document.memory_date, source_session_ids=(), document=document)
        return document_mappers.daily_memory_to_row(memory, character_id=owner, recall_tier=tier)
    evidence = EvidenceRef("private-session", "private-message", "private-quote")
    document = TopicDocument(f"主题-{identifier}", TopicIdentity("主题说明", (evidence,)),
        TopicCurrentState("进度", ("entry",)), (TopicEntry("entry", "记录", None, NOW.date(), (evidence,)),))
    memory = TopicMemory(document_id=identifier, kind=kind, current_revision=3,
        content_hash=document.content_hash(), vector_document_key="vector", published_at=NOW,
        updated_at=updated_at, source_refs=(evidence,), document=document)
    return document_mappers.topic_memory_to_row(memory, character_id=owner, recall_tier=tier)


@pytest_asyncio.fixture
async def query_case(tmp_path):
    database = SQLiteDatabase(tmp_path / "queries.sqlite3")
    try:
        await database.initialize()
        async with database.transaction() as session:
            session.add_all([CharacterRow(character_id=key, name=key, character_setting="test") for key in ("c", "other")])
            await session.flush()
            rows = [memory_row(MemoryKind.DAILY, "same"), memory_row(MemoryKind.DAILY, "z", day=5),
                    memory_row(MemoryKind.TOPIC, "same"), memory_row(MemoryKind.TOPIC, "z"),
                    memory_row(MemoryKind.TOPIC, "latest", updated_at=NOW + timedelta(hours=1)),
                    memory_row(MemoryKind.TOPIC, "archived", tier=RecallTier.ARCHIVED),
                    memory_row(MemoryKind.TOPIC, "foreign", owner="other")]
            for field in ("published_at", "document_json", "vector_document_key"):
                pending = memory_row(MemoryKind.TOPIC, "pending-" + field)
                setattr(pending, field, None)
                rows.append(pending)
            session.add_all(rows)
        repository = SQLiteMemoryEditRepository(database)
        yield database, repository, MemoryQueryService(repository)
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [("daily", ["same", "z"]), ("topic", ["latest", "z", "same", "archived"])])
async def test_separate_pages_keep_stable_order(query_case, kind, expected):
    _, repository, service = query_case
    list_rows = getattr(repository, f"list_{kind}_memories")
    list_page = getattr(service, f"list_{kind}_memories")
    full = await list_rows(character_id="c", limit=100)
    assert [row.document_id for row in full] == expected
    cursor, collected = None, []
    for _ in range(10):
        page = await list_page(character_id="c", limit=1, cursor=cursor)
        assert isinstance(page, Page)
        collected.extend(row.document_id for row in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        direction, field = ("after", "memory_date") if kind == "daily" else ("before", "updated_at")
        assert data["kind"] == kind
        assert set(data[direction]) == {field, "document_id"}
    assert collected == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,count", [(MemoryKind.DAILY, 2), (MemoryKind.TOPIC, 4)])
async def test_type_filter_and_formal_publication_conditions(query_case, kind, count):
    _, repository, service = query_case
    page = await getattr(service, f"list_{kind.value}_memories")(character_id="c")
    assert len(page.items) == count and all(row.kind == kind for row in page.items)
    assert page.next_cursor is None
    for field in ("published_at", "document_json", "vector_document_key"):
        assert await repository.get_memory(character_id="c", kind=MemoryKind.TOPIC,
            document_id="pending-" + field) is None
    assert (await service.list_topic_memories(character_id="other")).items[0].document_id == "foreign"
    assert (await service.list_topic_memories(character_id="unknown")).items == ()


@pytest.mark.asyncio
async def test_detail_is_scoped_by_role_and_kind_and_hides_evidence(query_case):
    _, _, service = query_case
    daily = await service.get_memory(character_id="c", kind=MemoryKind.DAILY, document_id="same")
    topic = await service.get_memory(character_id="c", kind=MemoryKind.TOPIC, document_id="same")
    assert daily.document.title == "日记-same" and topic.document.topic == "主题-same"
    assert (daily.revision, topic.revision) == (2, 3)
    assert "private-" not in json.dumps(asdict(topic), default=str)
    with pytest.raises(NotFoundError):
        await service.get_memory(character_id="c", kind=MemoryKind.TOPIC, document_id="foreign")
    with pytest.raises(NotFoundError):
        await service.get_memory(character_id="c", kind=MemoryKind.TOPIC, document_id="missing")


@pytest.mark.asyncio
async def test_deleted_cursor_boundary_does_not_prevent_continuation(query_case):
    database, _, service = query_case
    first = await service.list_topic_memories(character_id="c", limit=2)
    boundary = first.items[-1]
    async with database.transaction() as session:
        await session.execute(delete(TopicMemoryRow).where(TopicMemoryRow.topic_id == boundary.document_id))
    rest = await service.list_topic_memories(character_id="c", limit=100, cursor=first.next_cursor)
    assert [row.document_id for row in rest.items] == ["same", "archived"]


@pytest.mark.asyncio
async def test_cursor_is_bound_to_role_and_type_filter(query_case):
    _, _, service = query_case
    first = await service.list_topic_memories(character_id="c", limit=1)
    with pytest.raises(InvalidInputError, match="游标"):
        await service.list_topic_memories(character_id="other", cursor=first.next_cursor)
    with pytest.raises(InvalidInputError, match="游标"):
        await service.list_daily_memories(character_id="c", cursor=first.next_cursor)
    daily = await service.list_daily_memories(character_id="c", limit=1)
    with pytest.raises(InvalidInputError, match="游标"):
        await service.list_topic_memories(character_id="c", cursor=daily.next_cursor)


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["!", "", "null", "W10", "x" * 2049])
async def test_bad_cursor_rejected(query_case, cursor):
    _, _, service = query_case
    with pytest.raises(InvalidInputError):
        await service.list_topic_memories(character_id="c", cursor=cursor)


@pytest.mark.asyncio
async def test_detail_rejects_corrupted_hash(query_case):
    database, _, service = query_case
    async with database.transaction() as session:
        (await session.get(TopicMemoryRow, "same")).content_hash = "wrong"
    with pytest.raises(InvalidStateError, match="hash"):
        await service.get_memory(character_id="c", kind=MemoryKind.TOPIC, document_id="same")


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [dict(limit=0), dict(limit=101), dict(limit=True),
    dict(character_id="")])
async def test_invalid_parameters_rejected(query_case, arguments):
    _, _, service = query_case
    with pytest.raises(InvalidInputError):
        await service.list_topic_memories(**(dict(character_id="c") | arguments))


@pytest.mark.asyncio
async def test_daily_date_order_survives_edits_and_deleted_boundary(query_case):
    database, _, service = query_case
    async with database.transaction() as session:
        (await session.get(DailyMemoryRow, "same")).updated_at = NOW + timedelta(days=10)
    first = await service.list_daily_memories(character_id="c", limit=1)
    assert first.items[0].document_id == "same"
    assert first.items[0].memory_date == date(2026, 11, 4)
    async with database.transaction() as session:
        await session.execute(delete(DailyMemoryRow).where(DailyMemoryRow.daily_id == "same"))
    rest = await service.list_daily_memories(character_id="c", cursor=first.next_cursor)
    assert [row.document_id for row in rest.items] == ["z"]


@pytest.mark.asyncio
async def test_http_separate_lists_and_details(query_case):
    _, _, service = query_case
    app = create_app()
    app.state.container = SimpleNamespace(memory_query_service=service, memory_edit_service=MemoryEditService(service._repository))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        daily = await client.get("/api/v1/memories/daily", params={"character_id": "c", "limit": 1})
        assert daily.status_code == 200, daily.text
        assert daily.json()["items"][0]["memory_date"] == "2026-11-04"
        cursor = daily.json()["next_cursor"]
        following = await client.get("/api/v1/memories/daily", params={"character_id": "c", "cursor": cursor})
        assert [item["document_id"] for item in following.json()["items"]] == ["z"]
        topics = await client.get("/api/v1/memories/topic", params={"character_id": "c"})
        assert topics.status_code == 200, topics.text
        assert [item["document_id"] for item in topics.json()["items"]] == ["latest", "z", "same", "archived"]
        cross_cursor = await client.get("/api/v1/memories/topic", params={"character_id": "c", "cursor": cursor})
        assert cross_cursor.status_code == 422
        for kind in ("daily", "topic"):
            detail = await client.get(f"/api/v1/memories/{kind}/same", params={"character_id": "c"})
            assert detail.status_code == 200, detail.text
            assert detail.json()["kind"] == kind
            assert "private-" not in detail.text
            missing = await client.get(f"/api/v1/memories/{kind}/same", params={"character_id": "other"})
            assert missing.status_code == 404
        invalid = await client.get("/api/v1/memories/daily", params={"character_id": "c", "limit": 101})
        assert invalid.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,title_field", [("daily", "title"), ("topic", "topic")])
async def test_http_edit_saves_immediately_and_replays_idempotently(query_case, kind, title_field):
    _, _, service = query_case
    app = create_app()
    app.state.container = SimpleNamespace(memory_query_service=service, memory_edit_service=MemoryEditService(service._repository))
    path = f"/api/v1/memories/{kind}/same"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before = (await client.get(path, params={"character_id": "c"})).json()
        document = before["document"] | {title_field: "  用户修改的标题  "}
        payload = dict(character_id="c", expected_revision=before["revision"],
                       idempotency_key="edit-request", document=document)
        saved = await client.put(path, json=payload)
        assert saved.status_code == 200, saved.text
        result = saved.json()
        assert result["document_id"] == "same" and result["kind"] == kind
        assert result["revision"] == before["revision"] + 1
        assert result["index_status"] == "pending"
        after = (await client.get(path, params={"character_id": "c"})).json()
        assert after["document"] == document and after["revision"] == result["revision"]
        replay = await client.put(path, json=payload)
        assert replay.status_code == 200 and replay.json() == result
        stale = await client.put(path, json=payload | {"idempotency_key": "new-request"})
        assert stale.status_code == 409
        collision = await client.put(path, json=payload | {"document": document | {title_field: "不同标题"}})
        assert collision.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["daily", "topic"])
async def test_http_edit_validates_ownership_schema_and_document_existence(query_case, kind):
    _, _, service = query_case
    app = create_app()
    app.state.container = SimpleNamespace(memory_query_service=service, memory_edit_service=MemoryEditService(service._repository))
    path = f"/api/v1/memories/{kind}/same"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before = (await client.get(path, params={"character_id": "c"})).json()
        payload = dict(character_id="c", expected_revision=before["revision"],
                       idempotency_key="edit-request", document=before["document"])
        for changes in (dict(expected_revision=True), dict(unexpected="field"),
                        dict(document=before["document"] | {"source_refs": []})):
            assert (await client.put(path, json=payload | changes)).status_code == 422
        assert (await client.put(path, json=payload | {"character_id": "other"})).status_code == 404
        assert (await client.put(f"/api/v1/memories/{kind}/missing", json=payload)).status_code == 404
        if kind == "daily":
            bad_body = before["document"] | {"memory_date": "2020-01-01"}
        else:
            bad_body = before["document"] | {"entries": []}  # current_state仍引用被删除条目
        assert (await client.put(path, json=payload | {"document": bad_body})).status_code == 422
