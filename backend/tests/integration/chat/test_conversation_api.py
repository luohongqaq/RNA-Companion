"""真实 SQLite 验证创建空会话和首次发送的 HTTP 契约。"""

from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import base64
import json

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from api.app import create_app
from module.chat.conversation.core.processing.conversations import ConversationService
from module.chat.conversation.core.processing.messages import MessageService
from module.chat.reply.core.processing.query import GenerationQueryService
from module.shared.jobs.config.retry import RetryPolicy
from module.chat.reply.core.domain.models import GenerationFailure, GenerationFailureKind
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import (
    CharacterRow, ConversationRow, SessionRow, TurnRow, MessageRow,
    GenerationJobRow, RequestReceiptRow,
)


@pytest_asyncio.fixture
async def chat_api(tmp_path):
    database = SQLiteDatabase(tmp_path / "conversation.sqlite3")
    try:
        await database.initialize()
        async with database.transaction() as session:
            session.add(CharacterRow(character_id="character_test", name="测试角色",
                                     character_setting="测试"))
        app = create_app()
        # ASGITransport 不启动正式 lifespan；测试使用独立数据库且不启动模型。
        app.state.container = SimpleNamespace(
            conversation_service=ConversationService(SQLiteChatRepository(database)),
            message_service=MessageService(SQLiteChatRepository(database)),
        )
        app.state.container.generation_query_service = GenerationQueryService(
            SQLiteGenerationRepository(database), app.state.container.message_service,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            yield client, database
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_create_empty_conversation_then_send_idempotently(chat_api):
    client, database = chat_api
    response = await client.post("/api/v1/conversations", json={
        "character_id": "character_test", "title": " 日常聊天 ",
    })
    assert response.status_code == 201
    body = response.json()
    conversation_id = body["conversation_id"]
    assert response.headers["Location"] == f"/api/v1/conversations/{conversation_id}"
    assert body["title"] == "日常聊天"
    assert body["state"] == "active"
    assert body["current_session_id"] is None
    assert body["created_at"] == body["updated_at"]
    detail = await client.get(response.headers["Location"])
    assert detail.status_code == 200
    assert detail.json() == body
    assert "Location" not in detail.headers
    assert (await client.get(response.headers["Location"])).json() == body
    async with database.db_session() as session:
        assert await session.get(ConversationRow, conversation_id) is not None
        for model in (SessionRow, TurnRow, MessageRow, GenerationJobRow, RequestReceiptRow):
            assert await session.scalar(select(func.count()).select_from(model)) == 0

    payload = {"request_id": "request_first_message", "character_id": "character_test",
               "content": "你好"}
    path = f"/api/v1/conversations/{conversation_id}/messages"
    first = await client.post(path, json=payload)
    assert first.status_code == 202
    assert first.json()["save_state"] == "committed"
    retry = await client.post(path, json=payload)
    assert retry.status_code == 202
    assert retry.json() == first.json()
    async with database.db_session() as session:
        for model in (ConversationRow, SessionRow, TurnRow, MessageRow,
                      GenerationJobRow, RequestReceiptRow):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
        row = await session.get(ConversationRow, conversation_id)
        assert row.title == "日常聊天"
        assert row.current_session_id is not None
        updated_at = row.updated_at
        session_id = row.current_session_id
    detail = await client.get(response.headers["Location"])
    assert detail.status_code == 200
    assert detail.json()["current_session_id"] == session_id
    async with database.db_session() as session:
        row = await session.get(ConversationRow, conversation_id)
        assert row.updated_at == updated_at
        assert await session.scalar(select(func.count()).select_from(ConversationRow)) == 1


@pytest.mark.asyncio
async def test_get_missing_conversation_returns_404_without_writes(chat_api):
    client, database = chat_api
    response = await client.get("/api/v1/conversations/conv_missing")
    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "会话不存在"}
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(ConversationRow)) == 0


@pytest.mark.asyncio
async def test_get_archived_conversation(chat_api):
    client, database = chat_api
    created = await client.post("/api/v1/conversations", json={"character_id": "character_test"})
    conversation_id = created.json()["conversation_id"]
    async with database.transaction() as session:
        row = await session.get(ConversationRow, conversation_id)
        row.state = "archived"
    response = await client.get(created.headers["Location"])
    assert response.status_code == 200
    assert response.json()["state"] == "archived"
    assert response.json()["title"] is None


@pytest.mark.asyncio
async def test_create_without_title_and_unknown_character(chat_api):
    client, database = chat_api
    response = await client.post("/api/v1/conversations", json={"character_id": "character_test"})
    assert response.status_code == 201
    assert response.json()["title"] is None
    missing = await client.post("/api/v1/conversations", json={"character_id": "missing"})
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(ConversationRow)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {}, {"character_id": " "}, {"character_id": "x" * 129},
    {"character_id": "character_test", "title": " "},
    {"character_id": "character_test", "title": "x" * 201},
    {"character_id": "character_test", "conversation_id": "client_id"},
])
async def test_invalid_create_does_not_write(chat_api, payload):
    client, database = chat_api
    response = await client.post("/api/v1/conversations", json=payload)
    assert response.status_code == 422
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(ConversationRow)) == 0


async def seed_conversation_list(database, count=25):
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    async with database.transaction() as session:
        session.add(CharacterRow(character_id="other", name="另一角色", character_setting="测试"))
        await session.flush()
        for index in range(count):
            session.add(ConversationRow(
                conversation_id=f"conv_{index:03}",
                character_id="character_test" if index % 2 == 0 else "other",
                title=f"聊天 {index}", state="active" if index % 3 else "archived",
                current_session_id=None, created_at=now,
                updated_at=now + timedelta(seconds=index // 5), ended_at=None,
            ))


@pytest.mark.asyncio
async def test_list_defaults_and_stable_cursor_pagination(chat_api):
    client, database = chat_api
    empty = await client.get("/api/v1/conversations")
    assert empty.json() == {"items": [], "next_cursor": None}
    await seed_conversation_list(database)
    default = await client.get("/api/v1/conversations")
    assert default.status_code == 200
    assert len(default.json()["items"]) == 20
    assert default.json()["next_cursor"]
    seen, cursor = [], None
    while True:
        params = {"limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/api/v1/conversations", params=params)
        assert response.status_code == 200
        page = response.json()
        seen.extend(item["conversation_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert len(seen) <= 25
    assert seen == [f"conv_{index:03}" for index in reversed(range(25))]
    # 恰好填满一页时不能错误地发出下一页游标。
    all_rows = await client.get("/api/v1/conversations", params={"limit": 25})
    assert len(all_rows.json()["items"]) == 25
    assert all_rows.json()["next_cursor"] is None
    maximum = await client.get("/api/v1/conversations", params={"limit": 100})
    assert maximum.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("filters", [
    {"state": "active"}, {"state": "archived"}, {"character_id": "other"},
    {"state": "archived", "character_id": "character_test"},
    {"character_id": "missing"},
])
async def test_list_filters_across_pages(chat_api, filters):
    client, database = chat_api
    await seed_conversation_list(database)
    items, cursor = [], None
    while True:
        params = {**filters, "limit": 2}
        if cursor:
            params["cursor"] = cursor
        response = await client.get("/api/v1/conversations", params=params)
        assert response.status_code == 200
        page = response.json()
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert len(items) <= 25
    expected = [f"conv_{index:03}" for index in reversed(range(25))
                if ("state" not in filters or filters["state"] == ("active" if index % 3 else "archived"))
                and ("character_id" not in filters or filters["character_id"] == (
                    "character_test" if index % 2 == 0 else "other"))]
    assert [item["conversation_id"] for item in items] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 101}, {"limit": "bad"}, {"state": "unknown"},
    {"character_id": " "}, {"cursor": ""}, {"cursor": "invalid!"},
    {"cursor": base64.urlsafe_b64encode(b"[]").decode()},
    {"cursor": base64.urlsafe_b64encode(json.dumps({
        "v": 1, "time": "2026-11-03T00:00:00", "id": "conv_001",
    }).encode()).decode()},
])
async def test_invalid_list_parameters(chat_api, params):
    client, _database = chat_api
    response = await client.get("/api/v1/conversations", params=params)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_cursor_survives_deleted_anchor_and_rejects_changed_filters(chat_api):
    client, database = chat_api
    await seed_conversation_list(database)
    first = await client.get("/api/v1/conversations", params={"limit": 1})
    cursor = first.json()["next_cursor"]
    for filters in ({"state": "active"}, {"character_id": "other"}):
        response = await client.get("/api/v1/conversations", params={"cursor": cursor, **filters})
        assert response.status_code == 422
    async with database.transaction() as session:
        anchor = await session.get(ConversationRow, "conv_024")
        await session.delete(anchor)
    following = await client.get("/api/v1/conversations", params={"cursor": cursor, "limit": 100})
    assert following.status_code == 200
    assert [item["conversation_id"] for item in following.json()["items"]] == [
        f"conv_{index:03}" for index in reversed(range(24))
    ]


@pytest.mark.asyncio
async def test_message_history_across_sessions_and_turns(chat_api):
    client, database = chat_api
    created = await client.post("/api/v1/conversations", json={"character_id": "character_test"})
    conversation_id = created.json()["conversation_id"]
    path = f"/api/v1/conversations/{conversation_id}/messages"
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    expected = []
    visual = {"pose_name": {"pose_id": "p1", "pose_name": "站立"},
              "expression_name": {"expression_id": "e1", "expression_name": "微笑"}}
    async with database.transaction() as session:
        # ID 顺序与前驱顺序相反，开始时间相同，确保不依赖时间或随机 ID。
        for sid, predecessor in (("session_z", None), ("session_a", "session_z")):
            session.add(SessionRow(session_id=sid, conversation_id=conversation_id,
                                   session_seq=1 if predecessor is None else 2,
                                   predecessor_session_id=predecessor, session_state="sealed",
                                   session_start_time=now))
            await session.flush()
            for seq in (1, 2):
                turn_id = f"{sid}_turn_{seq}"
                turn = TurnRow(turn_id=turn_id, session_id=sid, turn_seq=seq,
                               turn_state="complete")
                session.add(turn)
                await session.flush()
                for msg_seq, role in ((1, "user"), (2, "user"), (3, "character"), (4, "character")):
                    message_id = f"{turn_id}_msg_{msg_seq}"
                    session.add(MessageRow(message_id=message_id, turn_id=turn_id,
                                           message_seq=msg_seq, role=role, content=message_id,
                                           visual_json=visual if msg_seq == 3 else None,
                                           created_at=now))
                    if msg_seq <= 3:
                        expected.append(message_id)
                await session.flush()
                turn.accepted_message_id = f"{turn_id}_msg_3"
    seen, cursor = [], None
    while True:
        params = {"limit": 5}
        if cursor:
            params["cursor"] = cursor
        response = await client.get(path, params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        ids = [item["message_id"] for item in page["items"]]
        seen = ids + seen
        for item in page["items"]:
            assert item["session_id"] in ("session_z", "session_a")
            assert item["turn_seq"] in (1, 2)
            assert "session_seq" not in item
            if item["role"] == "character":
                assert item["visual"] == visual
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert len(seen) <= 12
    assert seen == expected
    # 大页和刚好满页都不产生多余游标，默认按从早到晚返回最近历史。
    for params in ({}, {"limit": 12}, {"limit": 100}):
        page = (await client.get(path, params=params)).json()
        assert [item["message_id"] for item in page["items"]] == expected
        assert page["next_cursor"] is None
    first_page = (await client.get(path, params={"limit": 1})).json()
    token = first_page["next_cursor"]
    decoded = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    assert decoded["before"] == {
        "session_seq": 2, "session_id": "session_a", "turn_seq": 2, "message_seq": 3,
    }
    another = await client.post("/api/v1/conversations", json={"character_id": "character_test"})
    wrong = await client.get(
        f"/api/v1/conversations/{another.json()['conversation_id']}/messages",
        params={"cursor": first_page["next_cursor"]},
    )
    assert wrong.status_code == 422


@pytest.mark.asyncio
async def test_message_history_empty_missing_and_pending_input(chat_api):
    client, _database = chat_api
    assert (await client.get("/api/v1/conversations/missing/messages")).status_code == 404
    created = await client.post("/api/v1/conversations", json={"character_id": "character_test"})
    path = f"/api/v1/conversations/{created.json()['conversation_id']}/messages"
    response = await client.get(path)
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    sent = await client.post(path, json={"request_id": "request_history_test",
                                        "character_id": "character_test", "content": "你好"})
    assert sent.status_code == 202
    page = (await client.get(path)).json()
    assert len(page["items"]) == 1
    assert page["items"][0]["message_id"] == sent.json()["message_id"]
    assert page["items"][0]["content"] == "你好"
    assert page["items"][0]["visual"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 101}, {"limit": "x"}, {"cursor": ""},
    {"cursor": "invalid!"},
    {"cursor": base64.urlsafe_b64encode(json.dumps({
        "v": 1, "conversation_id": "conv_test", "before": [-1, "s", 1, 1],
    }).encode()).decode()},
    *[{"cursor": base64.urlsafe_b64encode(json.dumps({
        "v": 1, "conversation_id": "conv_test", "before": boundary,
    }).encode()).decode()} for boundary in (
        {},
        {"session_seq": -1, "session_id": "s", "turn_seq": 1, "message_seq": 1},
        {"session_seq": 0, "session_id": "s", "turn_seq": 0, "message_seq": 1},
        {"session_seq": 0, "session_id": "s", "turn_seq": 1, "message_seq": True},
    )],
])
async def test_invalid_message_history_parameters(chat_api, params):
    client, _database = chat_api
    response = await client.get("/api/v1/conversations/conv_test/messages", params=params)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_patch_title_archive_restore_and_preserve_pending_job(chat_api):
    client, database = chat_api
    created = await client.post("/api/v1/conversations", json={
        "character_id": "character_test", "title": "原标题",
    })
    path = created.headers["Location"]
    cid = created.json()["conversation_id"]
    payload = {"request_id": "patch_pending_message", "character_id": "character_test", "content": "你好"}
    sent = await client.post(path + "/messages", json=payload)
    assert sent.status_code == 202
    async with database.db_session() as session:
        original = await session.get(ConversationRow, cid)
        created_at, session_id = original.created_at, original.current_session_id
        job = await session.get(GenerationJobRow, sent.json()["job_id"])
        original_job_state = job.state

    renamed = await client.patch(path, json={"title": " 新标题 "})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "新标题"
    assert renamed.json()["state"] == "active"
    archived = await client.patch(path, json={"state": "archived"})
    assert archived.status_code == 200
    assert archived.json()["title"] == "新标题"
    assert archived.json()["state"] == "archived"
    repeated = await client.patch(path, json={"state": "archived"})
    assert repeated.json() == archived.json()
    blocked = await client.post(path + "/messages", json={**payload, "request_id": "patch_blocked_message"})
    assert blocked.status_code == 409
    # 归档后仍可重放保存回执，也可以读取历史和重命名。
    assert (await client.post(path + "/messages", json=payload)).json() == sent.json()
    history = await client.get(path + "/messages")
    assert history.status_code == 200 and len(history.json()["items"]) == 1
    assert (await client.patch(path, json={"title": "归档聊天"})).status_code == 200
    filtered = await client.get("/api/v1/conversations", params={"state": "archived"})
    assert [item["conversation_id"] for item in filtered.json()["items"]] == [cid]
    async with database.db_session() as session:
        row = await session.get(ConversationRow, cid)
        assert row.ended_at is not None
        assert row.created_at == created_at and row.current_session_id == session_id
        assert row.updated_at > row.created_at
        assert (await session.get(GenerationJobRow, sent.json()["job_id"])).state == original_job_state
        assert await session.scalar(select(func.count()).select_from(MessageRow)) == 1
    restored = await client.patch(path, json={"state": "active", "title": None})
    assert restored.status_code == 200
    assert restored.json()["title"] is None
    assert restored.json()["state"] == "active"
    async with database.db_session() as session:
        assert (await session.get(ConversationRow, cid)).ended_at is None
    resent = await client.post(path + "/messages", json={**payload, "request_id": "patch_after_restore"})
    assert resent.status_code == 202


@pytest.mark.asyncio
async def test_patch_empty_conversation_and_missing_id(chat_api):
    client, database = chat_api
    missing = await client.patch("/api/v1/conversations/missing", json={"title": "新标题"})
    assert missing.status_code == 404
    created = await client.post("/api/v1/conversations", json={"character_id": "character_test"})
    path = created.headers["Location"]
    response = await client.patch(path, json={"state": "archived", "title": "空聊天"})
    assert response.status_code == 200
    assert response.json()["current_session_id"] is None
    assert response.json()["title"] == "空聊天"
    async with database.db_session() as session:
        for model in (SessionRow, TurnRow, MessageRow, GenerationJobRow):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {}, {"title": " "}, {"title": "x" * 201}, {"title": 123},
    {"state": None}, {"state": "deleted"}, {"character_id": "other"},
    {"title": "新标题", "state": "bad"},
])
async def test_invalid_patch_does_not_modify_conversation(chat_api, payload):
    client, _database = chat_api
    created = await client.post("/api/v1/conversations", json={"character_id": "character_test"})
    path = created.headers["Location"]
    response = await client.patch(path, json=payload)
    assert response.status_code == 422
    assert (await client.get(path)).json() == created.json()


async def make_failed_generation(client, database):
    created = await client.post("/api/v1/conversations", json={"character_id": "character_test"})
    path = created.headers["Location"]
    sent = await client.post(path + "/messages", json={
        "request_id": "failed_reply_request", "character_id": "character_test", "content": "你好",
    })
    job_id = sent.json()["job_id"]
    repository = SQLiteGenerationRepository(database)
    ticket = await repository.claim_job(job_id)
    assert ticket is not None
    failure = GenerationFailure(GenerationFailureKind.NETWORK, "timeout", "网络超时", True,
                                datetime.now(timezone.utc))
    assert await repository.record_failure(job_id, ticket.attempt_id, failure, RetryPolicy(max_retries=0))
    return path, job_id, repository, ticket, failure


@pytest.mark.asyncio
async def test_retry_failed_reply_requeues_once_and_rejects_old_attempt(chat_api):
    import asyncio
    client, database = chat_api
    path, job_id, repository, old_ticket, failure = await make_failed_generation(client, database)
    failed = await client.get(f"/api/v1/generation-jobs/{job_id}")
    assert failed.json()["status"] == "failed"
    assert failed.json()["manual_retry_count"] == 0
    assert failed.json()["error"]["code"] == "timeout"
    retry_path = f"/api/v1/generation-jobs/{job_id}/retry"
    responses = await asyncio.gather(client.post(retry_path), client.post(retry_path))
    assert all(response.status_code == 202 for response in responses)
    for response in responses:
        assert response.json()["status"] == "need_prepare"
        assert response.json()["error"] is None
        assert response.json()["reply"] is None
        assert response.json()["retry_count"] == 0
        assert response.json()["manual_retry_count"] == 1
        assert response.json()["job_id"] == job_id
    new_ticket = await repository.claim_job(job_id)
    assert new_ticket is not None
    assert new_ticket.attempt_id != old_ticket.attempt_id
    assert new_ticket.active_token > old_ticket.active_token
    assert await repository.claim_job(job_id) is None
    assert not await repository.record_failure(job_id, old_ticket.attempt_id, failure, RetryPolicy())
    assert (await client.post(retry_path)).status_code == 202
    async with database.db_session() as session:
        job = await session.get(GenerationJobRow, job_id)
        assert job.manual_retry_count == 1
        assert job.attempt_count == 2
        assert job.current_attempt_id == new_ticket.attempt_id
        assert job.finished_at is None
        for model in (MessageRow, GenerationJobRow, RequestReceiptRow):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
    queried = (await client.get(f"/api/v1/generation-jobs/{job_id}")).json()
    listed = (await client.get(path + "/generation-jobs")).json()["items"]
    assert queried["manual_retry_count"] == listed[0]["manual_retry_count"] == 1
    # 新尝试再次失败后允许用户再次手动重试。
    assert await repository.record_failure(job_id, new_ticket.attempt_id, failure, RetryPolicy(max_retries=0))
    retried_again = await client.post(retry_path)
    assert retried_again.status_code == 202
    assert retried_again.json()["manual_retry_count"] == 2
    assert len((await client.get(path + "/messages")).json()["items"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["archived", "new_input", "sealed", "complete", "obsolete", "cancelled"])
async def test_retry_rejects_ineligible_reply(chat_api, reason):
    client, database = chat_api
    path, job_id, repository, ticket, _failure = await make_failed_generation(client, database)
    if reason == "archived":
        assert (await client.patch(path, json={"state": "archived"})).status_code == 200
    elif reason == "new_input":
        assert (await client.post(path + "/messages", json={
            "request_id": "new_input_after_failure", "character_id": "character_test", "content": "补充",
        })).status_code == 202
    else:
        async with database.transaction() as session:
            job = await session.get(GenerationJobRow, job_id)
            turn = await session.get(TurnRow, ticket.turn_id)
            if reason == "sealed":
                (await session.get(SessionRow, turn.session_id)).session_state = "sealed"
            elif reason == "complete":
                turn.turn_state = "complete"
            else:
                job.state = reason
    before = await repository.get_job(job_id)
    response = await client.post(f"/api/v1/generation-jobs/{job_id}/retry")
    assert response.status_code == 409
    assert (await repository.get_job(job_id)) == before


@pytest.mark.asyncio
async def test_retry_missing_and_never_failed_jobs(chat_api):
    client, _database = chat_api
    assert (await client.post("/api/v1/generation-jobs/missing/retry")).status_code == 404
    created = await client.post("/api/v1/conversations", json={"character_id": "character_test"})
    sent = await client.post(created.headers["Location"] + "/messages", json={
        "request_id": "never_failed_request", "character_id": "character_test", "content": "你好",
    })
    assert (await client.post(f"/api/v1/generation-jobs/{sent.json()['job_id']}/retry")).status_code == 409


@pytest.mark.asyncio
async def test_conversation_job_lookup_empty_missing_and_scoped(chat_api):
    client, _database = chat_api
    assert (await client.get("/api/v1/conversations/missing/generation-jobs")).status_code == 404
    first = await client.post("/api/v1/conversations", json={"character_id": "character_test"})
    second = await client.post("/api/v1/conversations", json={"character_id": "character_test"})
    path = first.headers["Location"]
    assert (await client.get(path + "/generation-jobs")).json() == {"items": []}
    sent = await client.post(path + "/messages", json={
        "request_id": "lookup_first_message", "character_id": "character_test", "content": "你好",
    })
    response = await client.get(path + "/generation-jobs")
    assert response.status_code == 200
    assert response.json()["items"] == [
        (await client.get(f"/api/v1/generation-jobs/{sent.json()['job_id']}")).json()
    ]
    assert (await client.get(second.headers["Location"] + "/generation-jobs")).json() == {"items": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["need_prepare", "need_generate", "need_accept", "failed",
                                       "done", "obsolete", "cancelled"])
async def test_conversation_job_lookup_filters_states_and_keeps_delayed_jobs(chat_api, state):
    client, database = chat_api
    path, job_id, _repository, _ticket, _failure = await make_failed_generation(client, database)
    async with database.transaction() as session:
        job = await session.get(GenerationJobRow, job_id)
        job.state = state
        job.available_at = datetime.now(timezone.utc) + timedelta(hours=1)
    response = await client.get(path + "/generation-jobs")
    assert response.status_code == 200
    items = response.json()["items"]
    if state in ("done", "obsolete", "cancelled"):
        assert items == []
    else:
        assert len(items) == 1
        assert items[0]["job_id"] == job_id
        assert items[0]["status"] == state
        assert items[0]["reply"] is None
        assert items[0]["error"]["code"] == "timeout"


@pytest.mark.asyncio
async def test_conversation_job_lookup_excludes_old_input_and_old_session(chat_api):
    client, database = chat_api
    path, old_id, _repository, ticket, _failure = await make_failed_generation(client, database)
    sent = await client.post(path + "/messages", json={
        "request_id": "lookup_new_input", "character_id": "character_test", "content": "补充",
    })
    assert sent.status_code == 202
    items = (await client.get(path + "/generation-jobs")).json()["items"]
    assert [item["job_id"] for item in items] == [sent.json()["job_id"]]
    assert old_id != items[0]["job_id"]
    async with database.transaction() as session:
        turn = await session.get(TurnRow, ticket.turn_id)
        previous = await session.get(SessionRow, turn.session_id)
        previous.session_state = "sealed"
        await session.flush()
        now = datetime.now(timezone.utc)
        session.add(SessionRow(session_id="lookup_successor", conversation_id=previous.conversation_id,
                               session_seq=previous.session_seq + 1, predecessor_session_id=previous.session_id,
                               session_state="open", session_start_time=now))
        await session.flush()
        (await session.get(ConversationRow, previous.conversation_id)).current_session_id = "lookup_successor"
    assert (await client.get(path + "/generation-jobs")).json() == {"items": []}
