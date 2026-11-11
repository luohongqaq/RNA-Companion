"""回复模型适配，负责请求参数、超时、用量和传输错误。"""
import asyncio
from typing import Any

from module.chat.reply.core.domain.config.reply import ReplyModelConfig
from module.chat.reply.core.domain.errors.reply import ReplyModelUnavailable
from module.chat.reply.core.domain.materials import ModelUsage, PromptMessage, ReplyModelResponse


class ReplyModelRepository:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(self, messages: tuple[PromptMessage, ...], config: ReplyModelConfig) -> ReplyModelResponse:
        try:
            # 始终思考类模型不接受 thinking=disabled，此时改发 reasoning_effort 控制思考强度。
            options = ({"reasoning_effort": config.reasoning_effort}
                       if config.reasoning_effort is not None else
                       {"extra_body": {"thinking": {"type": "disabled"}}})
            response = await asyncio.wait_for(self._client.chat.completions.create(
                model=config.model_name,
                messages=[{"role": message.role, "content": message.content} for message in messages],
                temperature=config.temperature, max_tokens=config.max_output_tokens,
                response_format={"type": "json_object"},
                timeout=config.timeout_seconds, **options,
            ), timeout=config.timeout_seconds)
            choice = response.choices[0]
            usage = None
            if response.usage is not None:
                counts = (response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.total_tokens)
                if all(type(value) is int and value >= 0 for value in counts):
                    usage = ModelUsage(*counts)
            content = choice.message.content
            return ReplyModelResponse(content if isinstance(content, str) else "", choice.finish_reason,
                                      response.id, usage)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = getattr(exc, 'status_code', None)
            retryable = status is None or status in (408, 409, 429) or (isinstance(status, int) and status >= 500)
            raise ReplyModelUnavailable(f"回复模型请求未完成（{type(exc).__name__}）", retryable=retryable) from exc
