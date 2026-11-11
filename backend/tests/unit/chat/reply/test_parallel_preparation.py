import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from module.chat.reply.core.processing.preparation import GenerationPreparationService
from module.memory_recall.agent.core.domain.result import MemoryContext
from module.memory_recall.agent.core.domain.result import MemorySelection


@pytest.mark.asyncio
@pytest.mark.parametrize("scene_recall_enabled", [True, False])
async def test_scene_recall_runs_in_parallel_only_when_enabled(scene_recall_enabled):
    memory_started, scene_started = asyncio.Event(), asyncio.Event()
    memory = MemoryContext("attempt", "empty", MemorySelection((), (), False), (), "", "", "skipped",
                           "no_relevant_memory", (), "skill", "summary", "view", 0, ())

    async def prepare(*args):
        memory_started.set()
        if scene_recall_enabled:
            await scene_started.wait()
        return memory

    async def search(query):
        scene_started.set()
        await memory_started.wait()
        assert query.messages[-1].content == "本轮输入"
        return ("scene",)

    builder = Mock()
    builder.build.return_value = "frozen"
    resources = NS(character_card="card", scene_recall_enabled=scene_recall_enabled)
    service = GenerationPreparationService(None, prompt_resources=NS(load=AsyncMock(return_value=resources)),
                                           prompt_builder=builder, scene_recall=NS(search=search))
    service.prepare_memory = prepare
    ticket = NS(job_id="job", attempt_id="attempt", turn_id="turn", session_id="session",
                input_version=1, active_token=1, input_boundary=NS(character_id="character", session_id="session"))
    context = NS(inherited_context=None, complete_turns=(), current_input=NS(user_messages=(NS(content="本轮输入"),)))
    result = await asyncio.wait_for(service.assemble(ticket, context), 1)
    assert result.frozen_prompt == "frozen"
    assert memory_started.is_set()
    assert scene_started.is_set() is scene_recall_enabled
    assert builder.build.call_args.kwargs["scenes"] == (("scene",) if scene_recall_enabled else ())


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_cancel", [False, True])
async def test_failure_or_cancellation_cleans_up_other_branch(caller_cancel):
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def prepare(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def search(query):
        await started.wait()
        if caller_cancel:
            await asyncio.Event().wait()
        raise RuntimeError("scene failed")

    resources = NS(character_card="card", scene_recall_enabled=True)
    service = GenerationPreparationService(None, prompt_resources=NS(load=AsyncMock(return_value=resources)),
                                           prompt_builder=Mock(), scene_recall=NS(search=search))
    service.prepare_memory = prepare
    ticket = NS(input_boundary=NS(character_id="character"))
    context = NS(inherited_context=None, complete_turns=(), current_input=NS(user_messages=(NS(content="输入"),)))
    task = asyncio.create_task(service.assemble(ticket, context))
    await started.wait()
    if caller_cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if caller_cancel else RuntimeError):
        await asyncio.wait_for(task, 1)
    assert cleaned.is_set()
