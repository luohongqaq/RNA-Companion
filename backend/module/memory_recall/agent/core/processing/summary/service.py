"""单次记忆总结业务；输入转换为纯函数，模型调用通过端口。"""
from __future__ import annotations
import asyncio
import json
from module.memory_recall.agent.core.domain.model import MemoryModelRequest
from module.memory_recall.agent.core.domain.ports import MemoryModelPort
from module.memory_recall.agent.core.domain.summary_input import MemorySummaryInput
from time import perf_counter


class MemorySummaryService:
    def __init__(self, gateway: MemoryModelPort, model: str, prompt: str) -> None:
        self._gateway = gateway
        self._model = model
        self._prompt = prompt

    async def summarize(
        self, *, selection: dict, user_input: str, chat_context: list,
        deadline_at: float, target_deadline_at: float,
        max_output_tokens: int = 2048, character_context: dict | None = None,
    ) -> dict:
        if not selection.get("selected"):
            return {"status": "skipped", "summary": "", "seconds": 0.0}
        started = perf_counter()
        payload = MemorySummaryInput.build(selection, user_input, chat_context, character_context)
        payload["remaining_seconds"] = max(0.0, target_deadline_at - perf_counter())
        payload["time_guidance"] = "剩余秒数是软目标，请尽快提交完整总结；目标归零不表示取消或总结失败。"
        remaining = deadline_at - perf_counter()
        result = {"status": "incomplete", "summary": ""}
        try:
            if remaining <= 0:
                raise TimeoutError()
            response = await asyncio.wait_for(self._gateway.complete(MemoryModelRequest(
                model=self._model,
                messages=[{"role": "system", "content": self._prompt},
                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}],
                timeout_seconds=remaining, thinking_enabled=False,
                max_output_tokens=max_output_tokens,
            )), timeout=remaining)
            text = response.message.get("content")
            if response.finish_reason != "stop":
                result["error"] = "生成未完整结束"
            elif not isinstance(text, str) or not text.strip() or len(text) > 2400:
                result["error"] = "总结必须是非空且不超过2400字的文本"
            else:
                result.update(status="summarized", summary=text.strip())
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            result["error"] = "总结达到硬保护时限，保留原选择结果"
        except Exception as exc:
            result["error"] = type(exc).__name__
        result["seconds"] = perf_counter() - started
        return result
