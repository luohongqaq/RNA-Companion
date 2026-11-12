"""封存接口：走上下文交接、等待换窗口，并支持新建对话时预开窗口。"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from api.app import create_app
from module.chat.conversation.core.processing.conversations import ConversationService
from module.chat.conversation.core.processing.messages import MessageService
from module.chat.conversation.core.processing.seal import SessionSealService
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from module.chat.reply.core.processing.query import GenerationQueryService
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from module.shared.common.ids import IdGenerator
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow, ConversationRow, MessageRow, SessionRow, TurnRow
from module.chat.conversation.repository import mappers as conversation_mappers
from module.context_compaction.core.domain.job import ContextJobType, CreateContextJobCommand
from module.context_compaction.repository.sqlite import SQLiteContextMaintenanceRepository
from sqlalchemy import text
from module.chat.conversation.core.domain.models import Session


class StubHandoff:
    """替身：只记录调用，按需返回接替会话。"""

    def __init__(self, successor=None, status=None, degraded=None):
        self.successor = successor
        self.status = status
        self.degraded = degraded
        self.prepared = []

    async def prepare_handoff(self, session_id, through_turn_seq):
        self.prepared.append((session_id, through_turn_seq))
        return "job-1"

    async def commit_handoff(self, session_id):
        return None if self.successor is None else SimpleNamespace(session_id=self.successor)

    async def handoff_status(self, session_id):
        return self.status

    async def commit_degraded_handoff(self, session_id):
        return None if self.degraded is None else SimpleNamespace(session_id=self.degraded)


@pytest_asyncio.fixture
async def chat_api(tmp_path):
    database = SQLiteDatabase(tmp_path / "seal.sqlite3")
    try:
        await database.initialize()
        async with database.transaction() as session:
            session.add(CharacterRow(character_id="character_test", name="测试角色",
                                     character_setting="测试"))
        app = create_app()
        repository = SQLiteChatRepository(database)
        handoff = StubHandoff()
        app.state.container = SimpleNamespace(
            conversation_service=ConversationService(repository),
            message_service=MessageService(repository),
            session_seal_service=SessionSealService(repository, handoff, wait_seconds=2.0),
        )
        app.state.container.generation_query_service = GenerationQueryService(
            SQLiteGenerationRepository(database), app.state.container.message_service,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            yield client, database, handoff
    finally:
        await database.close()


async def open_conversation(client, *, request_id="request-0001"):
    created = await client.post("/api/v1/conversations", json={"character_id": "character_test"})
    assert created.status_code == 201
    conversation_id = created.json()["conversation_id"]
    sent = await client.post(f"/api/v1/conversations/{conversation_id}/messages", json={
        "request_id": request_id, "content": "今天想聊点别的", "character_id": "character_test",
    })
    assert sent.status_code == 202
    detail = await client.get(f"/api/v1/conversations/{conversation_id}")
    return conversation_id, detail.json()["current_session_id"], sent.json()["turn_id"]


async def current_session(client, conversation_id):
    detail = await client.get(f"/api/v1/conversations/{conversation_id}")
    return detail.json()["current_session_id"]


async def complete_turn(database, turn_id):
    """把这一轮标成完成并挂上正式回复：上下文登记要求完整轮有采用消息。"""
    async with database.transaction() as session:
        turn = await session.get(TurnRow, turn_id)
        reply_id = IdGenerator.new("msg")
        session.add(MessageRow(
            message_id=reply_id, turn_id=turn_id, message_seq=2, role="character",
            content="嗯。", action="", created_at=datetime.now(timezone.utc),
        ))
        await session.flush()
        turn.turn_state = "complete"
        turn.accepted_message_id = reply_id


@pytest.mark.asyncio
async def test_seal_uses_handoff_and_reports_successor(chat_api):
    client, database, handoff = chat_api
    conversation_id, session_id, turn_id = await open_conversation(client)
    await complete_turn(database, turn_id)
    handoff.successor = "session-next"

    response = await client.post(f"/api/v1/conversations/{conversation_id}/sessions/{session_id}/seal")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "conversation_id": conversation_id, "sealed_session_id": session_id,
        "current_session_id": "session-next", "pending": False,
    }
    # 交接任务按最后一轮冻结，会话进入等待交接，交给上下文流水线接手。
    assert handoff.prepared == [(session_id, 1)]
    async with database.db_session() as db:
        assert (await db.get(SessionRow, session_id)).session_state == "waiting_handoff"


@pytest.mark.asyncio
async def test_seal_waits_for_successor_then_reports_it(chat_api):
    client, database, handoff = chat_api
    conversation_id, session_id, turn_id = await open_conversation(client)
    await complete_turn(database, turn_id)

    async def rotate():
        await asyncio.sleep(0.3)
        successor_id = IdGenerator.new("session")
        async with database.transaction() as db:
            sealed = await db.get(SessionRow, session_id)
            sealed.session_state = "sealed"
            sealed.session_end_time = datetime.now(timezone.utc)
            await db.flush()
            successor = Session.open(successor_id, conversation_id, datetime.now(timezone.utc))
            successor.session_seq = 2
            db.add(conversation_mappers.session_to_row(successor))
            await db.flush()
            (await db.get(ConversationRow, conversation_id)).current_session_id = successor_id

    task = asyncio.create_task(rotate())
    response = await client.post(f"/api/v1/conversations/{conversation_id}/sessions/{session_id}/seal")
    await task
    assert response.status_code == 200
    body = response.json()
    assert body["pending"] is False
    assert body["current_session_id"] != session_id
    assert await current_session(client, conversation_id) == body["current_session_id"]


@pytest.mark.asyncio
async def test_seal_rejects_unfinished_reply(chat_api):
    client, _, _ = chat_api
    conversation_id, session_id, _ = await open_conversation(client)
    response = await client.post(f"/api/v1/conversations/{conversation_id}/sessions/{session_id}/seal")
    assert response.status_code == 409
    assert "未完成" in response.json()["message"]


@pytest.mark.asyncio
async def test_seal_rejects_archived_conversation(chat_api):
    client, database, _ = chat_api
    conversation_id, session_id, turn_id = await open_conversation(client)
    await complete_turn(database, turn_id)
    await client.patch(f"/api/v1/conversations/{conversation_id}", json={"state": "archived"})
    response = await client.post(f"/api/v1/conversations/{conversation_id}/sessions/{session_id}/seal")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_seal_rejects_stale_session(chat_api):
    client, database, _ = chat_api
    conversation_id, _, turn_id = await open_conversation(client)
    await complete_turn(database, turn_id)
    response = await client.post(f"/api/v1/conversations/{conversation_id}/sessions/not-current/seal")
    assert response.status_code == 409
    assert "刷新" in response.json()["message"]


@pytest.mark.asyncio
async def test_create_conversation_can_open_first_session(chat_api):
    client, database, _ = chat_api
    created = await client.post("/api/v1/conversations", json={
        "character_id": "character_test", "with_session": True,
    })
    assert created.status_code == 201
    body = created.json()
    assert body["current_session_id"]
    async with database.db_session() as db:
        row = await db.get(SessionRow, body["current_session_id"])
        assert row.session_state == "open" and row.session_seq == 1


@pytest.mark.asyncio
async def test_seal_rolls_back_when_summary_failed(chat_api):
    """摘要失败且兜底也交不出东西时，会话退回可继续聊天。"""
    client, database, handoff = chat_api
    conversation_id, session_id, turn_id = await open_conversation(client)
    await complete_turn(database, turn_id)
    handoff.status = "failed"
    handoff.degraded = None

    response = await client.post(f"/api/v1/conversations/{conversation_id}/sessions/{session_id}/seal")
    assert response.status_code == 409
    assert "恢复" in response.json()["message"]
    # 失败不锁死对话：会话退回可继续聊天，用户还能再封存一次。
    async with database.db_session() as db:
        assert (await db.get(SessionRow, session_id)).session_state == "open"
    assert (await client.post(f"/api/v1/conversations/{conversation_id}/messages", json={
        "request_id": "request-0002", "content": "还能继续聊吗", "character_id": "character_test",
    })).status_code == 202


@pytest.mark.asyncio
async def test_seal_switches_window_when_summary_failed_but_tail_available(chat_api):
    """摘要失败但还能兜底交接时，宁可丢摘要也要换窗口，不把用户锁在旧会话里。"""
    client, database, handoff = chat_api
    conversation_id, session_id, turn_id = await open_conversation(client)
    await complete_turn(database, turn_id)
    handoff.status = "failed"
    handoff.degraded = "session-degraded"

    response = await client.post(f"/api/v1/conversations/{conversation_id}/sessions/{session_id}/seal")
    assert response.status_code == 200
    body = response.json()
    assert body["current_session_id"] == "session-degraded"
    assert body["pending"] is False


@pytest.mark.asyncio
async def test_failed_handoff_is_revived_on_next_attempt(chat_api):
    client, database, _ = chat_api
    conversation_id, session_id, turn_id = await open_conversation(client)
    await complete_turn(database, turn_id)
    repository = SQLiteContextMaintenanceRepository(database)
    job_id = await repository.create_job(CreateContextJobCommand(
        job_type=ContextJobType.PREPARE_HANDOFF, session_id=session_id,
        frozen_through_turn_seq=1, priority=10, dedupe_key=f"prepare_handoff:{session_id}",
    ))
    async with database.transaction() as db:
        await db.execute(text("UPDATE context_jobs SET status='failed' WHERE job_id=:job"), {"job": job_id})

    assert await repository.handoff_status(session_id) == "failed"
    assert await repository.prepare_handoff(session_id, 1) == job_id
    # 失败任务重新排入待生成，重试次数清零，再次封存才有可能成功。
    assert await repository.handoff_status(session_id) == "need_generate"
