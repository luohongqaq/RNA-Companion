"""Adapt the model SDK to the recall model port."""
from __future__ import annotations
import asyncio
from module.memory_recall.agent.core.domain.model import MemoryModelRequest, MemoryModelResult
from typing import Any


class CompatibleMemoryModelGateway:
    def __init__(self, client: Any, *, send_thinking_parameter: bool = True,
                 always_thinking_effort: str | None = None) -> None:
        self._client = client
        self._send_thinking_parameter = send_thinking_parameter
        # 部分模型不支持关闭思考（thinking=disabled 会被拒绝），改发 reasoning_effort 压低思考。
        self._always_thinking_effort = always_thinking_effort

    async def complete(self, request: MemoryModelRequest) -> MemoryModelResult:
        params = dict(model=request.model, messages=request.messages,
                      max_tokens=request.max_output_tokens, timeout=request.timeout_seconds)
        if request.tools is not None:
            params.update(tools=request.tools, tool_choice="auto")
        if request.thinking_enabled:
            params["reasoning_effort"] = request.reasoning_effort
        elif self._always_thinking_effort is not None:
            params["reasoning_effort"] = self._always_thinking_effort
        if self._send_thinking_parameter and self._always_thinking_effort is None:
            params["extra_body"] = {"thinking": {"type": "enabled" if request.thinking_enabled else "disabled"}}
        response = await asyncio.wait_for(self._client.chat.completions.create(**params),
                                          timeout=request.timeout_seconds)
        choice = response.choices[0]
        message = choice.message.model_dump(mode="json", exclude_none=True)
        return MemoryModelResult(message=message, finish_reason=choice.finish_reason,
                                 request_id=response.id,
                                 usage=response.usage.model_dump() if response.usage is not None else {})
