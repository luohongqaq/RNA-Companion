"""长期记忆异常分流：只处理本次阶段，取消传播，已发布版本不重新生成。"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from module.shared.common.errors.base import InvalidStateError
from module.memory_management.shared.jobs.core.scheduling.workflow import MemoryWorkflow
from module.memory_management.shared.contracts.errors import MemoryModelUnavailable, MemorySourceChangedError
from module.memory_management.daily.core.domain.job import DailyJob, DailyJobInput
from module.memory_management.shared.jobs.core.domain.job import MemoryJobStatus as Status, MemoryJobType
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef


def job(status=Status.NEED_GENERATE, **changes):
    now = datetime.now(timezone.utc)-timedelta(seconds=1)
    return replace(DailyJob("j", MemoryJobType.CREATE_DAILY, "daily", "dedupe", 10,
        status, now, DailyJobInput("c", ("s",)), now, now), **changes)


def workflow(before, current, error):
    repository = SimpleNamespace(get_job=AsyncMock(side_effect=[before, current]),
        retry_job=AsyncMock(return_value=True), fail_job=AsyncMock(return_value=True))
    service = SimpleNamespace(process_job=AsyncMock(side_effect=error))
    return MemoryWorkflow(repository, service), repository, service


@pytest.mark.asyncio
@pytest.mark.parametrize("error,retry,replan", [
    (MemorySourceChangedError("来源变化"), True, True),
    (TimeoutError("private"), True, False),
    (ConnectionError("private"), True, False),
    (MemoryModelUnavailable("调用暂不可用", retryable=True), True, False),
    (MemoryModelUnavailable("调用凭证无效", retryable=False), False, False),
    (InvalidStateError("invalid"), False, False),
    (ValueError("private"), False, False),
    (RuntimeError("private"), False, False),
])
async def test_failure_classification_uses_committed_attempt(error, retry, replan):
    before = job()
    current = replace(before, status=Status.GENERATING, attempt_version=1)
    flow, repo, _ = workflow(before, current, error)
    assert await flow.execute("j")
    call = repo.retry_job.await_args if retry else repo.fail_job.await_args
    assert call.args[:2] == ("j", 1)
    assert call.kwargs["expected_status"] == Status.GENERATING
    assert call.kwargs["expected_retry_count"] == 0
    assert "private" not in (call.kwargs["error"] if retry else call.args[2])
    if retry:
        assert call.kwargs["restart_generation"] == replan
        assert call.kwargs["available_at"] > datetime.now(timezone.utc)
        repo.fail_job.assert_not_awaited()
    else:
        repo.retry_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [
    job(Status.NEED_REVIEW, attempt_version=1),
    job(Status.GENERATING, attempt_version=3),
    job(Status.NEED_GENERATE, retry_count=1),
    job(Status.DONE), None,
])
async def test_late_error_does_not_rewind_progress(current):
    flow, repo, _ = workflow(job(), current, TimeoutError())
    assert not await flow.execute("j")
    repo.retry_job.assert_not_awaited()
    repo.fail_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_exhaustion_finishes_task_without_extra_retry():
    before = job(Status.NEED_REVIEW, retry_count=3)
    flow, repo, _ = workflow(before, before, TimeoutError())
    assert await flow.execute("j")
    repo.fail_job.assert_awaited_once()
    repo.retry_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [Status.GENERATING, Status.REVIEWING, Status.WRITING])
async def test_orphan_running_stage_is_recovered_without_calling_service(status):
    before = job(status, attempt_version=2)
    flow, repo, service = workflow(before, before, None)
    assert await flow.execute("j")
    repo.retry_job.assert_awaited_once()
    service.process_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_changed", [True, False])
async def test_published_task_can_retry_export_but_never_replan(source_changed):
    before = job(Status.NEED_ACTIVATE, result=MemoryRevisionRef(MemoryKind.DAILY, "d", 1, "h"), applied_revision=1)
    error = MemorySourceChangedError("来源变化") if source_changed else TimeoutError()
    flow, repo, _ = workflow(before, before, error)
    assert await flow.execute("j")
    if source_changed:
        repo.fail_job.assert_awaited_once()
        repo.retry_job.assert_not_awaited()
    else:
        assert not repo.retry_job.await_args.kwargs["restart_generation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), NotImplementedError()])
async def test_cancel_and_unimplemented_scaffold_propagate(error):
    before = job()
    flow, repo, _ = workflow(before, before, error)
    with pytest.raises(type(error)):
        await flow.execute("j")
    repo.retry_job.assert_not_awaited()
    repo.fail_job.assert_not_awaited()
