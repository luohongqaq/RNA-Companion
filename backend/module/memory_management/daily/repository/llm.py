"""daily 模型外部调用适配，业务校验由 core 完成。"""
from __future__ import annotations
import asyncio
import json
from module.shared.common.errors.base import InvalidStateError
from module.memory_management.daily.core.processing.config import DailyModelConfig
from module.memory_management.shared.contracts.errors import MemoryModelUnavailable
from typing import Any


class DailyModelRepository:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(self, prompt: str, payload: dict, config: DailyModelConfig) -> dict:
        """生成和审核共用请求、超时与JSON边界，不持有数据库事务。"""
        options = {"extra_body": {"thinking": {"type": "disabled" if config.disable_thinking else "enabled"}}}
        if not config.disable_thinking:
            options["reasoning_effort"] = config.reasoning_effort
        try:
            response = await asyncio.wait_for(self._client.chat.completions.create(
                model=config.model_name,
                messages=[{"role": "system", "content": prompt},
                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
                response_format={"type": "json_object"}, max_tokens=config.max_output_tokens,
                timeout=config.timeout_seconds, **options,
            ), timeout=config.timeout_seconds)
        except Exception as error:
            status = getattr(error, "status_code", None)
            retryable = (isinstance(error, (TimeoutError, ConnectionError, OSError))
                or type(error).__name__ in ("APIConnectionError", "APITimeoutError")
                or (type(status) is int and (status in (408, 409, 429) or 500 <= status < 600)))
            raise MemoryModelUnavailable(f"daily模型调用未完成（{type(error).__name__}）",
                                         retryable=retryable) from error
        try:
            if not response.choices or response.choices[0].finish_reason != "stop":
                raise InvalidStateError("daily模型输出未正常结束，禁止保存截断结果")
            try:
                result = json.loads(response.choices[0].message.content)
            except (TypeError, json.JSONDecodeError) as error:
                raise InvalidStateError("daily模型未返回合法JSON") from error
            return result
        except InvalidStateError as error:
            raise MemoryModelUnavailable(str(error), retryable=True) from error
