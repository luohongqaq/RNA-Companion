"""上下文维护协程：串行、恢复、阶段退避、取消与运行时资源释放。"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from module.context_compaction.core.domain.errors import ContextModelUnavailable, ContextSourceChangedError
from module.context_compaction.core.domain.job import ContextJob, ContextJobStatus as Status, ContextJobType
from module.context_compaction.core.processing.service import ContextCompactionService
from module.context_compaction.repository.llm import ContextSummaryModelRepository
from module.context_compaction.core.domain.policy import ContextCompactionPolicy
from module.chat.reply.core.domain.config.context_budget import ContextPromptBudget
from module.context_compaction.core.scheduling.worker import ContextWorker
from module.context_compaction.core.scheduling.workflow import ContextWorkflow
from module.shared.jobs.config.retry import RetryPolicy


def job(status=Status.NEED_GENERATE, **changes):
    now = datetime.now(timezone.utc)
    value = ContextJob("j", ContextJobType.PREPARE_HANDOFF, "s", "session:s", "dedupe", 10,
        status, now, 1, now, now)
    return replace(value, **changes)


def repository(current):
    return SimpleNamespace(get_job=AsyncMock(return_value=current),
        get_next_executable_job=AsyncMock(return_value=None), recover_unfinished_attempts=AsyncMock(return_value=0),
        retry_job=AsyncMock(return_value=True), fail_job=AsyncMock(return_value=True))


async def execute(workflow, before):
    repo = workflow._repository
    repo.get_job.side_effect = [before, repo.get_job.return_value]
    return await workflow.execute(before.job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,retry,replan", [
    (TimeoutError(), True, False),
    (ContextModelUnavailable("network", retryable=True), True, False),
    (ContextModelUnavailable("authentication", retryable=False), False, False),
    (ContextSourceChangedError("source changed"), True, True),
    (ValueError("invalid configuration"), False, False),
    (RuntimeError("unexpected"), False, False),
])
async def test_generation_failure_uses_new_attempt_and_classifies_error(error, retry, replan):
    before = job()
    current = replace(before, status=Status.GENERATING, attempt_version=1)
    repo = repository(current)
    service = SimpleNamespace(process_job=AsyncMock(side_effect=error))
    workflow = ContextWorkflow(repo, service)
    assert await execute(workflow, before)
    if retry:
        repo.fail_job.assert_not_awaited()
        args = repo.retry_job.await_args
        assert args.args == ("j", 1)
        assert args.kwargs["expected_status"] == Status.GENERATING
        assert args.kwargs["restart_generation"] == replan
        assert args.kwargs["available_at"] > datetime.now(timezone.utc)
    else:
        repo.retry_job.assert_not_awaited()
        assert repo.fail_job.await_args.args[:2] == ("j", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [Status.NEED_VALIDATE, Status.NEED_PUBLISH])
async def test_later_stage_timeout_preserves_phase(status):
    current = job(status, attempt_version=4)
    repo = repository(current)
    workflow = ContextWorkflow(repo, SimpleNamespace(process_job=AsyncMock(side_effect=TimeoutError())))
    assert await execute(workflow, current)
    args = repo.retry_job.await_args
    assert args.args == ("j", 4) and args.kwargs["expected_status"] == status
    assert not args.kwargs["restart_generation"]


@pytest.mark.asyncio
async def test_retry_exhaustion_finishes_current_attempt():
    current = job(Status.NEED_VALIDATE, attempt_version=3, retry_count=3)
    repo = repository(current)
    workflow = ContextWorkflow(repo, SimpleNamespace(process_job=AsyncMock(side_effect=TimeoutError())))
    assert await execute(workflow, current)
    repo.retry_job.assert_not_awaited()
    assert repo.fail_job.await_args.args[:2] == ("j", 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [
    job(Status.NEED_VALIDATE, attempt_version=1),
    job(Status.GENERATING, attempt_version=2),
    job(Status.READY, attempt_version=1),
    job(Status.NEED_GENERATE, attempt_version=0, retry_count=1),
])
async def test_exception_after_committed_progress_or_new_attempt_does_not_rewind(current):
    repo = repository(current)
    workflow = ContextWorkflow(repo, SimpleNamespace(process_job=AsyncMock(side_effect=TimeoutError())))
    assert not await execute(workflow, job())
    repo.fail_job.assert_not_awaited()
    repo.retry_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_handled_invalid_output_is_not_counted_twice():
    before = job()
    repo = repository(replace(before, attempt_version=1, retry_count=1))
    workflow = ContextWorkflow(repo, SimpleNamespace(process_job=AsyncMock()))
    assert await execute(workflow, before)
    repo.retry_job.assert_not_awaited()
    repo.fail_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_serial_worker_can_recover_runtime_orphan_without_startup_scan():
    current = job(Status.GENERATING, attempt_version=2)
    repo = repository(current)
    service = SimpleNamespace(process_job=AsyncMock())
    assert await execute(ContextWorkflow(repo, service), current)
    service.process_job.assert_not_awaited()
    repo.retry_job.assert_awaited_once()
    repo.recover_unfinished_attempts.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_recovers_once_and_does_not_dispatch_during_active_call():
    current = job()
    repo = repository(replace(current, status=Status.NEED_VALIDATE, attempt_version=1))
    entered, release, rescanned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def scan():
        assert repo.recover_unfinished_attempts.await_count == 1
        if repo.get_next_executable_job.await_count == 1:
            return current
        rescanned.set()
        return None

    async def execute(_):
        entered.set()
        await release.wait()

    repo.get_next_executable_job.side_effect = scan
    worker = ContextWorker(repo, ContextWorkflow(repo, SimpleNamespace(process_job=AsyncMock(side_effect=execute))), poll_interval=60)
    await worker.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await worker.start()
        worker.notify()
        assert repo.get_next_executable_job.await_count == 1
        release.set()
        await asyncio.wait_for(rescanned.wait(), 2)
        repo.recover_unfinished_attempts.assert_awaited_once()
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_stop_cancels_model_call_without_failure_and_restart_recovers_again():
    repo = repository(job())
    entered, cancelled = asyncio.Event(), asyncio.Event()
    repo.get_next_executable_job.return_value = job()

    async def execute(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker = ContextWorker(repo, ContextWorkflow(repo, SimpleNamespace(process_job=AsyncMock(side_effect=execute))))
    await worker.start()
    await asyncio.wait_for(entered.wait(), 2)
    await worker.stop()
    assert cancelled.is_set()
    repo.fail_job.assert_not_awaited()
    repo.retry_job.assert_not_awaited()
    entered.clear()
    await worker.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert repo.recover_unfinished_attempts.await_count == 2
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_scan_failure_does_not_kill_worker_and_notify_wakes_it():
    repo = repository(job())
    failed, retried = asyncio.Event(), asyncio.Event()

    async def scan():
        if repo.get_next_executable_job.await_count == 1:
            failed.set()
            raise RuntimeError("database temporarily unavailable")
        retried.set()
        return None

    repo.get_next_executable_job.side_effect = scan
    worker = ContextWorker(repo, ContextWorkflow(repo, SimpleNamespace(process_job=AsyncMock())), poll_interval=60)
    await worker.start()
    try:
        await asyncio.wait_for(failed.wait(), 2)
        worker.notify()
        await asyncio.wait_for(retried.wait(), 2)
    finally:
        await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,retryable", [(401, False), (429, True), (503, True)])
async def test_model_http_failure_is_normalized_without_sdk_import(status, retryable):
    class ApiError(Exception):
        status_code = status

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=ApiError("private")))))
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy(), model=ContextSummaryModelRepository(client))
    with pytest.raises(ContextModelUnavailable) as caught:
        await service._request_model(prompt="JSON", payload={}, model="test")
    assert caught.value.retryable == retryable and "private" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_start", [False, True])
async def test_application_context_only_mode_stops_worker_before_resources(tmp_path, monkeypatch, fail_start):
    import api.container as container_module
    from storage.config.settings import Settings
    from storage.llm import client_factory as client_factory_module

    order = []
    client = SimpleNamespace(close=AsyncMock(side_effect=lambda: order.append("client")))
    worker = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("recovery failed") if fail_start else None),
        stop=AsyncMock(side_effect=lambda: order.append("worker")))
    client_factory, worker_factory = Mock(return_value=client), Mock(return_value=worker)
    monkeypatch.setattr(client_factory_module, "AsyncOpenAI", client_factory)
    monkeypatch.setattr(container_module, "ContextWorker", worker_factory)
    settings = Settings(_env_file=None, data_root=tmp_path, DEFAULT_API_KEY="test-key",
        context_enabled=True, generation_enabled=False, memory_enabled=False, reply_enabled=False)
    container = container_module.build_container(settings)
    container.token_counter.load_directory = Mock()
    if fail_start:
        with pytest.raises(RuntimeError, match="recovery failed"):
            await container.start()
    else:
        try:
            await container.start()
            worker.start.assert_awaited_once()
            assert container.generation_worker is None
            assert worker_factory.call_args.args[1]._service._prompt_resources is not None
        finally:
            await container.stop()
    assert order == ["worker", "client"]
    client.close.assert_awaited_once()
