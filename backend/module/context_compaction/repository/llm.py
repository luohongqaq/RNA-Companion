"""摘要 JSON 请求适配，负责超时、传输错误和响应解析。"""
from __future__ import annotations
import asyncio
import json
from typing import Any
from module.shared.common.errors.base import InvalidStateError
from module.context_compaction.core.domain.policy import ContextModelConfig
from module.context_compaction.core.domain.errors import ContextModelUnavailable


class ContextSummaryModelRepository:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(self, *, prompt: str, payload: dict, model: str, config: ContextModelConfig) -> dict:
        """独立JSON请求；不持有数据库事务，失败由任务执行器安排恢复。"""
        options = {"extra_body": {"thinking": {"type": "disabled" if config.disable_thinking else "enabled"}}}
        if not config.disable_thinking:
            options["reasoning_effort"] = config.reasoning_effort
        try:
            response = await asyncio.wait_for(self._client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": prompt},
                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
                response_format={"type": "json_object"},
                max_tokens=config.max_output_tokens, timeout=config.timeout_seconds, **options,
            ), timeout=config.timeout_seconds)
        except Exception as error:
            status = getattr(error, "status_code", None)
            retryable = (isinstance(error, (TimeoutError, ConnectionError, OSError))
                or type(error).__name__ in ("APIConnectionError", "APITimeoutError")
                or (type(status) is int and (status in (408, 409, 429) or 500 <= status < 600)))
            raise ContextModelUnavailable(f"摘要模型调用未完成（{type(error).__name__}）",
                retryable=retryable) from error
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise InvalidStateError("上下文模型输出未正常结束，禁止使用截断摘要")
        try:
            result = json.loads(response.choices[0].message.content)
        except (TypeError, json.JSONDecodeError) as error:
            raise InvalidStateError("上下文模型未返回合法JSON") from error
        if not isinstance(result, dict):
            raise InvalidStateError("上下文模型输出必须为JSON对象")
        return result
