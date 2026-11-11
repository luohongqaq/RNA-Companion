"""聊天事件订阅：只交付业务事件，SSE 编码由 API 层处理。"""

import asyncio
from collections.abc import AsyncIterator
from math import isfinite
from time import monotonic

from module.chat.conversation.core.domain.ports import ChatRepository
from module.chat.conversation.core.domain.events import ChatEvent
from module.shared.common.errors.base import InvalidInputError


class ChatEventService:
    """按游标补发、持续读取，并在空闲时交付心跳标记 None。"""
    def __init__(
        self, repository: ChatRepository, *, batch_size: int = 100,
        poll_interval: float = 1.0, heartbeat_interval: float = 15.0,
    ) -> None:
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise ValueError("batch_size 必须为 1～100")
        for name, value in (("poll_interval", poll_interval), ("heartbeat_interval", heartbeat_interval)):
            if type(value) not in (int, float) or not isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须为有限正数")
        self._repository = repository
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._heartbeat_interval = heartbeat_interval

    @staticmethod
    def validate_cursor(event_id: int) -> None:
        if type(event_id) is not int or not 0 <= event_id < 2**63:
            raise InvalidInputError("event_id 必须为非负 SQLite 整数")

    async def get_after_event(self, event_id: int) -> tuple[ChatEvent, ...]:
        """获取指定事件之后的事件"""
        self.validate_cursor(event_id)
        return await self._repository.list_events_after(event_id, limit=self._batch_size)

    async def subscribe(self, after_event_id: int = 0) -> AsyncIterator[ChatEvent | None]:
        """每个订阅维护独立游标；取消/关闭订阅不会取消生成任务。

        None 仅表示心跳，不推进游标。仓储错误直接传播，不能伪装成空闲。
        异步生成器首次迭代时才执行，HTTP 层应先调用 validate_cursor 校验参数。
        """
        self.validate_cursor(after_event_id)
        cursor = after_event_id
        # 下一次应该发送或检查心跳的时间点
        heartbeat_at = monotonic() + self._heartbeat_interval
        while True:
            events = await self.get_after_event(cursor)
            if events:
                for event in events:
                    cursor = event.event_id
                    heartbeat_at = monotonic() + self._heartbeat_interval
                    yield event
                # 每处理完一批事件后，强制让事件循环调度一次。避免“快速消费者”或“积压事件”长时间独占事件循环。
                await asyncio.sleep(0)
                continue
            now = monotonic()
            if now >= heartbeat_at:
                heartbeat_at = now + self._heartbeat_interval
                yield None
            else:
                # 等待发生在仓储查询结束后，不长期占用数据库连接或事务。
                await asyncio.sleep(min(self._poll_interval, heartbeat_at - now))

