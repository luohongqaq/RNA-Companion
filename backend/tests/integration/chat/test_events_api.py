"""SSE 帧格式、重连游标和真实 ASGI 断开行为。"""
import asyncio
from datetime import datetime, timezone
import json

import httpx
import pytest

from api.app import create_app
from api.dependencies import get_chat_event_service
from module.chat.conversation.core.processing.events import ChatEventService
from module.chat.conversation.core.domain.contracts import AcceptedReply
from module.chat.conversation.core.domain.events import ReplyCommittedEvent


def event(number):
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    return ReplyCommittedEvent(number, now, f"job_{number}", AcceptedReply(
        "conv", "turn", f"message_{number}", "完整回复\n\nevent: fake", None, now,
    ))


class EventService:
    validate_cursor = staticmethod(ChatEventService.validate_cursor)

    def __init__(self, *, forever=False, fail=False):
        self.cursors = []
        self.closed = 0
        self.forever, self.fail = forever, fail

    async def subscribe(self, cursor):
        self.cursors.append(cursor)
        try:
            yield None
            for number in (1, 3, 7):
                if number > cursor:
                    yield event(number)
            if self.fail:
                raise RuntimeError("subscription unavailable")
            if self.forever:
                await asyncio.Event().wait()
        finally:
            self.closed += 1


def make_app(service):
    app = create_app()
    app.dependency_overrides[get_chat_event_service] = lambda: service
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("query,headers,expected", [
    ({}, {}, [1, 3, 7]),
    ({"after": 1}, {}, [3, 7]),
    ({"after": 0}, {"Last-Event-ID": "3"}, [7]),
    ({"after": 7}, {"Last-Event-ID": "0"}, [1, 3, 7]),
    ({"after": 100}, {}, []),
])
async def test_sse_format_and_resume_cursor(query, headers, expected):
    service = EventService()
    app = make_app(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/events", params=query, headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text.startswith("retry: 3000\n\n: heartbeat\n\n")
    frames = [frame for frame in response.text.split("\n\n") if frame.startswith("id:")]
    assert len(frames) == len(expected)
    for frame, number in zip(frames, expected):
        lines = frame.splitlines()
        assert len(lines) == 3  # JSON 内的换行不能注入新的 SSE 事件。
        assert lines[0] == f"id: {number}"
        assert lines[1] == "event: reply_committed"
        data = json.loads(lines[2].removeprefix("data: "))
        assert data["event_id"] == number
        assert data["job_id"] == f"job_{number}"
        assert data["reply"]["content"] == "完整回复\n\nevent: fake"
        assert data["reply"]["visual"] is None
    assert service.closed == 1
    assert service.cursors == [int(headers.get("Last-Event-ID", query.get("after", 0)))]
    schema = app.openapi()
    assert "text/event-stream" in schema["paths"]["/api/v1/events"]["get"]["responses"]["200"]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query,headers", [
    ({"after": -1}, {}), ({"after": "bad"}, {}), ({"after": 2**63}, {}),
    ({}, {"Last-Event-ID": "-1"}), ({}, {"Last-Event-ID": "bad"}),
    ({}, {"Last-Event-ID": ""}), ({}, {"Last-Event-ID": str(2**63)}),
])
async def test_invalid_cursor_returns_422_before_streaming(query, headers):
    service = EventService()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(service)), base_url="http://test") as client:
        response = await client.get("/api/v1/events", params=query, headers=headers)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert service.cursors == []


async def call_stream(app, *, disconnect=False, broken_send=False, spec_version=None):
    closed = asyncio.Event()
    messages = []
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec_version or ("2.4" if broken_send else "2.3")},
             "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/api/v1/events",
             "raw_path": b"/api/v1/events", "query_string": b"", "headers": [],
             "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8765), "root_path": ""}

    async def receive():
        await closed.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if b"event: reply_committed" in message.get("body", b""):
            if broken_send:
                raise OSError("client disconnected")
            if disconnect:
                closed.set()
                await asyncio.sleep(0)

    await asyncio.wait_for(app(scope, receive, send), timeout=2)
    return messages


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_disconnect_closes_infinite_subscription_without_cancelling_other_work(spec_version):
    service = EventService(forever=True)
    other_done = asyncio.Event()
    generation = asyncio.create_task(other_done.wait())
    try:
        await call_stream(make_app(service), disconnect=True, spec_version=spec_version)
        assert service.closed == 1
        assert not generation.done()
    finally:
        other_done.set()
        await generation


@pytest.mark.asyncio
async def test_send_error_closes_subscription():
    from starlette.requests import ClientDisconnect
    service = EventService(forever=True)
    with pytest.raises(ClientDisconnect):
        await call_stream(make_app(service), broken_send=True)
    assert service.closed == 1


@pytest.mark.asyncio
async def test_subscription_error_is_not_hidden_and_closes_generator():
    service = EventService(fail=True)
    with pytest.raises(RuntimeError, match="subscription unavailable"):
        await call_stream(make_app(service))
    assert service.closed == 1


@pytest.mark.asyncio
async def test_generation_failed_frame_and_resume():
    from module.chat.conversation.core.domain.events import GenerationFailedEvent
    from module.chat.reply.core.domain.models import GenerationFailure, GenerationFailureKind

    now = datetime.now(timezone.utc)
    class FailedService(EventService):
        async def subscribe(self, cursor):
            if cursor < 8:
                yield GenerationFailedEvent(8, now, "job", "conv", "turn", 1, 2,
                    GenerationFailure(GenerationFailureKind.NETWORK, "timeout",
                                      "超时\n\nevent: fake", True, now))
    app = make_app(FailedService())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/events?after=7")
        resumed = await client.get("/api/v1/events", headers={"Last-Event-ID": "8"})
    lines = response.text.split("\n\n")[1].splitlines()
    assert len(lines) == 3 and lines[:2] == ["id: 8", "event: generation_failed"]
    data = json.loads(lines[2].removeprefix("data: "))
    assert data["manual_retry_count"] == 2 and data["turn_id"] == "turn"
    assert data["error"]["code"] == "timeout"
    assert data["error"]["message"] == "超时\n\nevent: fake"
    assert resumed.text == "retry: 3000\n\n"
