"""三个任务执行器共享的启动恢复和串行生命周期。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from module.shared.jobs.worker import Worker
from module.context_compaction.core.scheduling.worker import ContextWorker
from module.chat.reply.core.scheduling.worker import GenerationWorker
from module.memory_management.daily.core.scheduling.worker import DailyWorker
from module.memory_management.object.core.scheduling.worker import ObjectWorker
from module.memory_management.topic.core.scheduling.worker import TopicWorker

PRODUCTION_WORKERS = (DailyWorker, ObjectWorker, TopicWorker)

def production_service():
    return SimpleNamespace(register_daily_jobs=AsyncMock(), register_object_jobs=AsyncMock(),
                           register_topic_jobs=AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ContextWorker, GenerationWorker, *PRODUCTION_WORKERS])
async def test_recovery_precedes_scan_and_start_is_idempotent(kind):
    entered, release, rescanned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    recovery = AsyncMock(return_value=0)
    scans = 0

    async def scan():
        nonlocal scans
        if kind is GenerationWorker:
            recovery.assert_not_awaited()
        else:
            recovery.assert_awaited_once()
        scans += 1
        if scans == 1:
            return (SimpleNamespace(job_id="j"),) if kind is GenerationWorker else SimpleNamespace(job_id="j")
        rescanned.set()
        return () if kind is GenerationWorker else None

    async def execute(_):
        entered.set()
        await release.wait()
        return True

    repository = SimpleNamespace(recover_unfinished_attempts=recovery,
        get_next_executable_job=AsyncMock(side_effect=scan),
        get_next_ready_job=AsyncMock(side_effect=scan), list_ready_jobs=AsyncMock(side_effect=scan))
    workflow = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    worker = kind(repository, workflow, production_service(), poll_interval=60) if kind in PRODUCTION_WORKERS else kind(repository, workflow, poll_interval=60)
    assert isinstance(worker, Worker)
    await asyncio.gather(worker.start(), worker.start())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        worker.notify()
        await worker.start()
        assert scans == 1
        release.set()
        await asyncio.wait_for(rescanned.wait(), 2)
        workflow.execute.assert_awaited_once_with("j")
    finally:
        await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ContextWorker, *PRODUCTION_WORKERS])
async def test_failed_recovery_propagates_without_starting_loop(kind):
    repository = SimpleNamespace(recover_unfinished_attempts=AsyncMock(side_effect=RuntimeError("recovery")))
    worker = kind(repository, SimpleNamespace(execute=AsyncMock()), production_service()) if kind in PRODUCTION_WORKERS else kind(repository, SimpleNamespace(execute=AsyncMock()))
    with pytest.raises(RuntimeError, match="recovery"):
        await worker.start()
    assert worker._task is None
    await worker.stop()


@pytest.mark.asyncio
async def test_generation_starts_without_repository_recovery_method():
    scanned = asyncio.Event()

    async def scan():
        scanned.set()
        return ()

    repository = SimpleNamespace(list_ready_jobs=AsyncMock(side_effect=scan))
    worker = GenerationWorker(repository, SimpleNamespace(execute=AsyncMock()))
    await worker.start()
    try:
        await asyncio.wait_for(scanned.wait(), 2)
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_restart_waits_for_cancelled_coroutine_to_exit():
    entered, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class PausingWorker(Worker):
        def __init__(self):
            super().__init__(name="test")
            self.recoveries = 0

        async def recover(self):
            self.recoveries += 1

        async def process_next(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopping.set()
                await release.wait()
            return False

    worker = PausingWorker()
    await worker.start()
    await asyncio.wait_for(entered.wait(), 2)
    stop = asyncio.create_task(worker.stop())
    await asyncio.wait_for(stopping.wait(), 2)
    restart = asyncio.create_task(worker.start())
    try:
        await asyncio.sleep(0)
        assert worker.recoveries == 1 and not restart.done()
        release.set()
        await asyncio.wait_for(stop, 2)
        await asyncio.wait_for(restart, 2)
        assert worker.recoveries == 2
    finally:
        release.set()
        await worker.stop()


@pytest.mark.asyncio
async def test_memory_registers_sealed_sessions_before_reading_ready_job():
    order=[]
    async def register():
        order.append("register")
        return ("daily",)
    async def scan():
        order.append("scan")
        return SimpleNamespace(job_id="daily")
    repository=SimpleNamespace(get_next_ready_job=AsyncMock(side_effect=scan))
    workflow=SimpleNamespace(execute=AsyncMock(return_value=True))
    worker=DailyWorker(repository,workflow,SimpleNamespace(register_daily_jobs=AsyncMock(side_effect=register)))
    assert await worker.process_next()
    assert order==["register","scan"]
    workflow.execute.assert_awaited_once_with("daily")
