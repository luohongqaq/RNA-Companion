"""topic 模型外部调用适配，业务校验由 core 完成。"""
from __future__ import annotations
import asyncio
import json
from module.shared.common.errors.base import InvalidStateError
from module.memory_management.shared.contracts.errors import MemoryModelUnavailable
from module.memory_management.topic.core.processing.config import TopicModelConfig
from typing import Any


class TopicModelRepository:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(self, prompt: str, payload: dict, config: TopicModelConfig) -> dict:
        try:
            response = await asyncio.wait_for(self._client.chat.completions.create(
                model=config.model_name, reasoning_effort=config.reasoning_effort,
                extra_body={"thinking": {"type": "enabled"}},
                messages=[{"role": "system", "content": prompt},
                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
                response_format={"type": "json_object"},
                max_tokens=config.max_output_tokens, timeout=config.timeout_seconds,
            ), timeout=config.timeout_seconds)
        except Exception as error:
            status = getattr(error, "status_code", None)
            retryable = (isinstance(error, (TimeoutError, ConnectionError, OSError))
                or type(error).__name__ in ("APIConnectionError", "APITimeoutError")
                or (type(status) is int and (status in (408, 409, 429) or 500 <= status < 600)))
            raise MemoryModelUnavailable(f"topic模型调用未完成（{type(error).__name__}）", retryable=retryable) from error
        try:
            if not response.choices or response.choices[0].finish_reason != "stop":
                raise InvalidStateError("topic模型输出未正常结束，禁止保存截断结果")
            content = response.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise InvalidStateError("topic模型返回空JSON内容，需要重试")
            try:
                data = json.loads(content)
            except json.JSONDecodeError as error:
                raise InvalidStateError(f"topic模型JSON语法错误：第{error.lineno}行第{error.colno}列，{error.msg}") from error
            return data
        except (InvalidStateError, TypeError, ValueError) as error:
            raise MemoryModelUnavailable(str(error), retryable=True) from error
