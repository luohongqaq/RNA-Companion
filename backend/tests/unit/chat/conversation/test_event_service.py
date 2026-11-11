"""订阅的补发、等待、心跳、隔离和取消语义。"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from module.chat.conversation.core.processing.events import ChatEventService
from module.chat.conversation.core.domain.contracts import AcceptedReply
from module.chat.conversation.core.domain.events import ReplyCommittedEvent
from module.shared.common.errors.base import InvalidInputError, InvalidStateError


def make_event(event_id):
    now = datetime.now(timezone.utc)
    return ReplyCommittedEvent(event_id, now, f"job_{event_id}", AcceptedReply(
        "conv", "turn", f"message_{event_id}", "完整回复", None, now,
    ))


class EventRepository:
    def __init__(self, ids=()):
        self.events = [make_event(value) for value in ids]
        self.calls = []

    async def list_events_after(self, event_id, *, limit):
        self.calls.append((event_id, limit))
        return tuple(event for event in self.events if event.event_id > event_id)[:limit]


@pytest.mark.asyncio
async def test_subscription_drains_backlog_in_order_without_duplicates():
    repository = EventRepository((1, 3, 7))
    service = ChatEventService(repository, batch_size=2)
    subscription = service.subscribe(0)
    try:
        events = [await anext(subscription) for _ in range(3)]
        assert [event.event_id for event in events] == [1, 3, 7]
        assert repository.calls == [(0, 2), (3, 2)]
    finally:
        await subscription.aclose()
    assert await service.get_after_event(3) == (repository.events[-1],)


@pytest.mark.asyncio
async def test_idle_heartbeat_keeps_cursor_and_delivers_later_event():
    repository = EventRepository()
    service = ChatEventService(repository, poll_interval=0.002, heartbeat_interval=0.01)
    subscription = service.subscribe(3)
    try:
        assert await asyncio.wait_for(anext(subscription), timeout=1) is None
        assert 2 <= len(repository.calls) <= 30
        assert all(cursor == 3 for cursor, _limit in repository.calls)
        repository.events.append(make_event(7))
        event = await asyncio.wait_for(anext(subscription), timeout=1)
        assert event.event_id == 7
        assert await asyncio.wait_for(anext(subscription), timeout=1) is None
        assert repository.calls[-1][0] == 7
    finally:
        await subscription.aclose()


@pytest.mark.asyncio
async def test_subscribers_have_independent_cursors():
    service = ChatEventService(EventRepository((1, 3, 7)))
    first, second = service.subscribe(0), service.subscribe(3)
    try:
        assert (await anext(first)).event_id == 1
        assert (await anext(second)).event_id == 7
        assert (await anext(first)).event_id == 3
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_cancel_idle_subscription_does_not_cancel_other_work():
    queried = asyncio.Event()
    finish_generation = asyncio.Event()

    async def read(*args, **kwargs):
        queried.set()
        return ()

    repository = EventRepository()
    repository.list_events_after = AsyncMock(side_effect=read)
    service = ChatEventService(repository, poll_interval=60, heartbeat_interval=60)
    subscription = service.subscribe()
    generation = asyncio.create_task(finish_generation.wait())
    waiting = asyncio.create_task(anext(subscription))
    try:
        await asyncio.wait_for(queried.wait(), timeout=1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not generation.done()
        repository.list_events_after.assert_awaited_once_with(0, limit=100)
    finally:
        await subscription.aclose()
        finish_generation.set()
        await generation


@pytest.mark.asyncio
async def test_repository_error_propagates_instead_of_becoming_heartbeat():
    repository = EventRepository()
    repository.list_events_after = AsyncMock(side_effect=InvalidStateError("快照损坏"))
    subscription = ChatEventService(repository).subscribe()
    with pytest.raises(InvalidStateError, match="快照损坏"):
        await anext(subscription)


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [-1, True, "1", None, 2**63])
async def test_invalid_cursor_does_not_query(cursor):
    repository = EventRepository()
    service = ChatEventService(repository)
    with pytest.raises(InvalidInputError):
        await service.get_after_event(cursor)
    with pytest.raises(InvalidInputError):
        await anext(service.subscribe(cursor))
    assert repository.calls == []


@pytest.mark.parametrize("options", [
    {"batch_size": 0}, {"batch_size": 101}, {"batch_size": True},
    {"poll_interval": 0}, {"poll_interval": float("inf")}, {"poll_interval": "1"},
    {"heartbeat_interval": -1}, {"heartbeat_interval": float("nan")}, {"heartbeat_interval": True},
])
def test_invalid_subscription_settings(options):
    with pytest.raises(ValueError):
        ChatEventService(EventRepository(), **options)
