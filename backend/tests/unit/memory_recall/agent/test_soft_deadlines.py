"""软目标只催促；硬保护前返回的有效选择/总结不被判超时。"""
import asyncio
import json
from types import SimpleNamespace

import pytest


class MaterialCounter:
    def count_text(self, text):
        return len(text)


from module.memory_recall.agent.core.processing.service import MemoryRecallService
from module.memory_recall.agent.core.processing.summary.service import MemorySummaryService
from module.memory_recall.agent.core.processing.selection.service import MemorySelectionService
from module.memory_recall.retrieval.core.processing.session import MemoryReadSession
from module.memory_recall.agent.core.processing.selection.service import MemorySelectionService
from module.memory_recall.agent.core.domain.model import MemoryModelResult
from module.memory_recall.agent.core.domain.state import AttemptState


def test_model_sees_soft_target_but_transport_uses_double_limit(monkeypatch):
    import module.memory_recall.agent.core.domain.state as state_module
    now = [100.0]
    monkeypatch.setattr(state_module.time, "monotonic", lambda: now[0])
    state = AttemptState.init(10, 6, None)
    attempt = MemorySelectionService(SimpleNamespace(), state)
    assert state.remaining() == 20
    assert attempt._policy()["remaining_seconds"] == 10
    now[0] = 111
    assert state.remaining() == 9
    assert attempt._policy()["remaining_seconds"] == 0
    assert state.must_force_finish(1)


@pytest.mark.asyncio
async def test_pipeline_accepts_selection_after_soft_limit():
    async def select(allowance):
        assert .09 < allowance <= .1
        await asyncio.sleep(.14)
        return {"reason_code": "no_relevant_memory", "selected": [], "unresolved": []}

    result = await MemoryRecallService._run_pipeline(select=select, user_input="你好", chat_context=[], summary_service=None, total_seconds=.1, target_seconds=.09, selection_limit=.1)
    assert result["completed"]
    assert result["over_soft_limit"] and not result["over_deadline"]


@pytest.mark.asyncio
async def test_outer_hard_limit_cancels_unfinished_selection():
    cancelled = []

    async def select(allowance):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    result = await MemoryRecallService._run_pipeline(select=select, user_input="你好", chat_context=[], summary_service=None, total_seconds=.1, target_seconds=.09, selection_limit=.05)
    assert not result["completed"] and cancelled
    assert "硬保护" in result["selection"]["unresolved"][0]


@pytest.mark.asyncio
async def test_summary_can_finish_after_global_soft_limit(monkeypatch):
    from module.memory_recall.agent.core.domain.summary_input import MemorySummaryInput
    monkeypatch.setattr(MemorySummaryInput, "build", staticmethod(lambda *args: {}))

    async def select(allowance):
        return {"reason_code": "selected", "selected": [{"title": "记忆"}], "unresolved": []}

    class Model:
        async def complete(self, request):
            assert request.timeout_seconds > .1
            await asyncio.sleep(.14)
            return MemoryModelResult({"content": "完整总结"}, "stop")

    result = await MemoryRecallService._run_pipeline(select=select, user_input="你好", chat_context=[],
        summary_service=MemorySummaryService(Model(), "summary", "总结"), total_seconds=.1, target_seconds=.09, selection_limit=.1)
    assert result["completed"] and result["result"]["summary"] == "完整总结"
    assert result["over_soft_limit"] and not result["over_deadline"]


@pytest.mark.asyncio
async def test_inflight_model_is_not_cancelled_at_soft_target():
    class Reader:
        async def temporal_coverage(self):
            return {}

    class Model:
        async def complete(self, request):
            updates = [json.loads(m["content"]) for m in request.messages
                       if m.get("content", "").startswith('{"kind": "runtime_budget_update"')]
            soft_remaining = updates[-1]["POLICY"]["remaining_seconds"]
            assert request.timeout_seconds > soft_remaining + .2
            await asyncio.sleep(soft_remaining + .03)
            return MemoryModelResult({"role": "assistant", "tool_calls": [{"id": "finish", "function": {
                "name": "finish_selection", "arguments": json.dumps({"reason_code": "no_relevant_memory", "selected": [], "unresolved": []})}}]}, "tool_calls")

    result = await MemorySelectionService.select(user_input="你好", chat_context=[], instructions="选择", store=MemoryReadSession(Reader()),
                                 client=Model(), model="test", token_counter=MaterialCounter(), deadline_seconds=.25)
    assert result["reason_code"] == "no_relevant_memory", result


@pytest.mark.asyncio
async def test_caller_cancellation_still_propagates():
    async def select(allowance):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await MemoryRecallService._run_pipeline(select=select, user_input="你好", chat_context=[], summary_service=None)
