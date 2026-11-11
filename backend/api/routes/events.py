"""应用级事件流：通知已提交的正式回复、最终失败和主动取消。"""
from contextlib import aclosing
from typing import Annotated

import asyncio
import anyio
from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from api.dependencies import get_chat_event_service
from api.schemas.events import (
    ReplyCommittedEventResponse, GenerationFailedEventResponse, GenerationCancelledEventResponse,
)
from module.chat.conversation.core.domain.events import ReplyCommittedEvent, GenerationFailedEvent, GenerationCancelledEvent
from module.chat.conversation.core.processing.events import ChatEventService
from logger import get_logger

router = APIRouter(prefix="/events", tags=["events"])
log = get_logger(__name__)


class EventStreamResponse(StreamingResponse):
    """同时监听断开和发送；不依赖服务器在 send 时抛异常才能结束订阅。"""
    media_type = "text/event-stream"

    async def __call__(self, scope, receive, send):
        tasks = []
        try:
            # 独立监听断开；使用一次性 asyncio 取消，允许数据库 finally 正常清理。
            streaming = asyncio.create_task(self.stream_response(send))
            tasks.append(streaming)
            disconnected = asyncio.create_task(self.listen_for_disconnect(receive))
            tasks.append(disconnected)
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            try:
                await (streaming if streaming in done else disconnected)
            except OSError as error:
                raise ClientDisconnect() from error
        finally:
            with anyio.CancelScope(shield=True):
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    await close()
        if self.background is not None:
            await self.background()


@router.get("", response_class=EventStreamResponse, responses={
    200: {"description": "SSE 事件流：reply_committed、generation_failed、generation_cancelled；data 为对应 JSON",
          "content": {"text/event-stream": {"schema": {"type": "string"}}}},
})
async def subscribe_events(
    service: Annotated[ChatEventService, Depends(get_chat_event_service)],
    after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID", ge=0, le=2**63 - 1)] = None,
) -> EventStreamResponse:
    """从 Last-Event-ID（优先）或 after 之后补发；默认从 0 补发全部保留事件。"""
    cursor = last_event_id if last_event_id is not None else after
    service.validate_cursor(cursor)  # 在响应头提交前完成参数校验。

    async def stream():
        log.info("SSE 订阅已建立", extra={"stage": "sse", "event": "subscription_started"})
        try:
            async with aclosing(service.subscribe(cursor)) as subscription:
                # 立即发送首帧，让客户端确认连接已建立。
                yield "retry: 3000\n\n"
                async for event in subscription:
                    if event is None:
                        yield ": heartbeat\n\n"
                        continue
                    if isinstance(event, ReplyCommittedEvent):
                        payload = ReplyCommittedEventResponse.model_validate(event)
                    elif isinstance(event, GenerationFailedEvent):
                        payload = GenerationFailedEventResponse.model_validate(event)
                    elif isinstance(event, GenerationCancelledEvent):
                        payload = GenerationCancelledEventResponse.model_validate(event)
                    else:
                        raise TypeError(f"不支持的事件类型：{type(event).__name__}")
                    yield (f"id: {payload.event_id}\n"
                           f"event: {event.event_type}\n"
                           f"data: {payload.model_dump_json()}\n\n")
        except Exception:
            log.error("SSE 订阅异常", exc_info=True, extra={"stage": "sse", "event": "subscription_failed"})
            raise
        finally:
            log.info("SSE 订阅已关闭", extra={"stage": "sse", "event": "subscription_closed"})

    return EventStreamResponse(stream(), headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    })
