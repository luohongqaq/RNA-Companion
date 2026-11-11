"""事件序号读取、分页及正式回复快照还原。"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from module.chat.conversation.core.domain.contracts import SaveMessageInput
from module.chat.conversation.core.domain.events import ReplyCommittedEvent
from module.chat.conversation.core.processing.events import ChatEventService
from module.shared.common.errors.base import InvalidStateError
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow, ChatEventRow, MessageRow, TurnRow, GenerationJobRow


@pytest_asyncio.fixture
async def event_store(tmp_path):
    database = SQLiteDatabase(tmp_path / "events.sqlite3")
    repository = SQLiteChatRepository(database)
    now = datetime(2026, 11, 3, 20, tzinfo=timezone(timedelta(hours=8)))
    try:
        await database.initialize()
        async with database.transaction() as session:
            session.add(CharacterRow(character_id="c", name="test", character_setting="test"))
        for event_id in (1, 3, 7):
            saved = await repository.accept_message(SaveMessageInput(
                f"request_event_{event_id}", f"conv_{event_id}", "c", "用户输入",
            ))
            visual = None if event_id == 1 else {
                "pose_name": {"pose_id": "p", "pose_name": "站立"},
                "expression_name": {"expression_id": "e", "expression_name": "微笑"},
            }
            async with database.transaction() as session:
                message_id = f"reply_{event_id}"
                session.add(MessageRow(message_id=message_id, turn_id=saved.turn_id, message_seq=2,
                                       role="character", content=f"完整回复 {event_id}",
                                       visual_json=visual, created_at=now))
                await session.flush()
                turn = await session.get(TurnRow, saved.turn_id)
                turn.turn_state, turn.accepted_message_id = "complete", message_id
                (await session.get(GenerationJobRow, saved.job_id)).state = "done"
                session.add(ChatEventRow(
                    event_id=event_id, event_type="reply_committed", conversation_id=f"conv_{event_id}",
                    job_id=saved.job_id, message_id=message_id, created_at=now - timedelta(seconds=event_id),
                    reply_json={"conversation_id": f"conv_{event_id}", "turn_id": saved.turn_id,
                                "message_id": message_id, "content": f"完整回复 {event_id}",
                                "visual": visual, "created_at": now.isoformat()},
                ))
        yield database, repository, now
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_events_order_limit_and_reply_roundtrip(event_store):
    database, repository, now = event_store
    events = await repository.list_events_after(0)
    assert isinstance(events, tuple)
    assert [event.event_id for event in events] == [1, 3, 7]
    assert all(isinstance(event, ReplyCommittedEvent) for event in events)
    assert events[0].reply.visual is None
    assert events[1].reply.visual.pose_name.pose_name == "站立"
    assert events[1].reply.visual.expression_name.expression_id == "e"
    assert events[1].reply.content == "完整回复 3"
    assert events[1].reply.conversation_id == "conv_3"
    assert events[1].reply.message_id == "reply_3"
    assert events[1].reply.created_at == now
    assert events[1].reply.created_at.tzinfo == timezone.utc
    assert events[1].created_at == now - timedelta(seconds=3)
    assert [event.event_id for event in await repository.list_events_after(0, limit=2)] == [1, 3]
    assert [event.event_id for event in await repository.list_events_after(3)] == [7]
    assert await repository.list_events_after(7) == ()
    assert await repository.list_events_after(1000) == ()
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(ChatEventRow)) == 3


@pytest.mark.asyncio
async def test_event_cursor_does_not_require_existing_anchor(event_store):
    database, repository, _now = event_store
    assert [event.event_id for event in await repository.list_events_after(2)] == [3, 7]
    async with database.transaction() as session:
        await session.delete(await session.get(ChatEventRow, 1))
    assert [event.event_id for event in await repository.list_events_after(1)] == [3, 7]
    cursor, seen = 0, []
    while events := await repository.list_events_after(cursor, limit=1):
        seen.extend(event.event_id for event in events)
        cursor = events[-1].event_id
    assert seen == [3, 7]


@pytest.mark.asyncio
async def test_subscription_replays_real_persisted_events(event_store):
    _database, repository, _now = event_store
    subscription = ChatEventService(repository, batch_size=1).subscribe(1)
    try:
        events = [await anext(subscription), await anext(subscription)]
        assert [event.event_id for event in events] == [3, 7]
        assert events[0].reply.content == "完整回复 3"
    finally:
        await subscription.aclose()


@pytest.mark.asyncio
async def test_event_read_uses_snapshot_not_current_message(event_store):
    database, repository, _now = event_store
    async with database.transaction() as session:
        (await session.get(MessageRow, "reply_1")).content = "后续变更"
    assert (await repository.list_events_after(0, limit=1))[0].reply.content == "完整回复 1"


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"message_id": "foreign"}, {"conversation_id": "foreign"}, {"turn_id": ""},
    {"created_at": "2026-11-03T12:00:00"}, {"visual": {}}, {"content": " "},
])
async def test_malformed_event_snapshot_is_rejected(event_store, override):
    database, repository, _now = event_store
    async with database.transaction() as session:
        row = await session.get(ChatEventRow, 1)
        row.reply_json = {**row.reply_json, **override}
    with pytest.raises(InvalidStateError, match="快照损坏"):
        await repository.list_events_after(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_id,limit", [
    (-1, 100), (True, 100), ("1", 100), (2**63, 100), (0, 0), (0, 101), (0, True), (0, "10"),
])
async def test_invalid_event_cursor_or_limit_does_not_query(event_id, limit):
    with pytest.raises(ValueError):
        await SQLiteChatRepository(object()).list_events_after(event_id, limit=limit)


@pytest.mark.asyncio
async def test_empty_event_store_returns_tuple(tmp_path):
    database = SQLiteDatabase(tmp_path / "empty.sqlite3")
    try:
        await database.initialize()
        assert await SQLiteChatRepository(database).list_events_after(0) == ()
    finally:
        await database.close()
