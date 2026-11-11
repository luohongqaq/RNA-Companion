"""长期记忆任务读取：到期、跨类型队首、退避和终态释放。"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from module.memory_management.shared.jobs.core.scheduling.workflow import MemoryWorkflow
from module.memory_management.daily.core.domain.job import DailyJob, DailyJobInput
from module.memory_management.topic.core.domain.job import TopicJob, TopicJobInput
from module.memory_management.shared.jobs.core.domain.job import MemoryJobStatus as Status, MemoryJobType
from module.memory_management.shared.jobs.repository import mappers as job_mappers
from storage.sqlite.db_session import SQLiteDatabase
from module.memory_management.shared.jobs.repository.sqlite import MemoryJobRepository


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(tmp_path / "scheduling.sqlite3")
    try:
        await database.initialize()
        yield database, MemoryJobRepository(database)
    finally:
        await database.close()


def job(job_id="j", *, target="daily:c:2026-10-30", **changes):
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    job_type = changes.pop("job_type", MemoryJobType.CREATE_DAILY)
    job_class, input_class = ((DailyJob, DailyJobInput) if job_type == MemoryJobType.CREATE_DAILY else (TopicJob, TopicJobInput))
    value = job_class(job_id, job_type, target, "dedupe:"+job_id, 10,
        Status.NEED_GENERATE, now, input_class("c", ("s",)), now, now)
    return replace(value, **changes)


async def save(database, *jobs):
    async with database.transaction() as session:
        session.add_all(job_mappers.memory_job_to_row(value) for value in jobs)


@pytest.mark.asyncio
async def test_get_job_and_scan_are_read_only(store):
    database, repo = store
    assert await repo.get_job("missing") is None
    assert await repo.get_next_ready_job() is None
    value = job(retry_count=2, attempt_version=3, attempt_count=2, last_error="previous")
    await save(database, value)
    assert await repo.get_job(value.job_id) == value
    assert await repo.get_next_ready_job() == value
    assert await repo.get_next_ready_job() == value
    assert await repo.get_job(value.job_id) == value


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [Status.NEED_GENERATE, Status.GENERATING, Status.NEED_REVIEW,
                                   Status.REVIEWING, Status.WRITING,
                                   Status.NEED_WRITE, Status.NEED_ACTIVATE])
async def test_all_executable_stages_are_selected(store, status):
    database, repo = store
    value = job(status=status)
    await save(database, value)
    assert await repo.get_next_ready_job() == value


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [Status.WAITING_INDEX, Status.DONE, Status.FAILED, Status.OBSOLETE])
async def test_waiting_and_terminal_are_readable_but_not_executable(store, status):
    database, repo = store
    value = job(status=status)
    await save(database, value)
    assert await repo.get_job(value.job_id) == value
    assert await repo.get_next_ready_job() is None


@pytest.mark.asyncio
async def test_future_head_blocks_its_target_but_not_others(store):
    database, repo = store
    head = job("head", available_at=datetime.now(timezone.utc) + timedelta(hours=1))
    tail = job("tail", created_at=head.created_at + timedelta(seconds=1), priority=0)
    other = job("other", target="daily:other:2026-10-30", priority=20)
    await save(database, head, tail, other)
    assert await repo.get_next_ready_job() == other


@pytest.mark.asyncio
async def test_waiting_index_blocks_later_job_of_different_type(store):
    database, repo = store
    head = job("head", target="topic:t", status=Status.WAITING_INDEX, job_type=MemoryJobType.CREATE_TOPIC)
    tail = job("tail", target="topic:t", job_type=MemoryJobType.UPDATE_TOPIC,
        created_at=head.created_at + timedelta(seconds=1), priority=0)
    await save(database, head, tail)
    assert await repo.get_next_ready_job() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [Status.DONE, Status.FAILED, Status.OBSOLETE])
async def test_terminal_head_releases_target(store, status):
    database, repo = store
    head = job("head", status=status)
    tail = job("tail", created_at=head.created_at + timedelta(seconds=1))
    await save(database, head, tail)
    assert await repo.get_next_ready_job() == tail


@pytest.mark.asyncio
async def test_same_target_order_uses_created_at_then_id_not_priority(store):
    database, repo = store
    head = job("a", priority=20)
    tail = job("b", created_at=head.created_at, priority=0)
    await save(database, tail, head)
    assert await repo.get_next_ready_job() == head


@pytest.mark.asyncio
async def test_different_targets_use_priority_then_created_at_then_id(store):
    database, repo = store
    first = job("a", target="a", priority=1)
    second = job("b", target="b", priority=1, created_at=first.created_at)
    newer = job("newer", target="newer", priority=1, created_at=first.created_at+timedelta(seconds=1))
    lower = job("lower", target="lower", priority=2, created_at=first.created_at-timedelta(seconds=1))
    await save(database, lower, second, newer, first)
    assert await repo.get_next_ready_job() == first


@pytest.mark.asyncio
async def test_workflow_dispatches_typed_job(store):
    database, repo = store
    value = job()
    await save(database, value)
    service = SimpleNamespace(process_job=AsyncMock())
    assert not await MemoryWorkflow(repo, service).execute(value.job_id)
    service.process_job.assert_awaited_once_with(value.job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [Status.GENERATING, Status.REVIEWING, Status.WRITING])
async def test_running_head_preserves_target_position(store, status):
    database, repo = store
    head = job("head", status=status)
    tail = job("tail", created_at=head.created_at+timedelta(seconds=1), priority=0)
    await save(database, head, tail)
    assert await repo.get_next_ready_job() == head


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["daily", "object", "topic"])
async def test_workers_scan_and_recover_only_own_family(store, family):
    from module.memory_management.object.core.domain.job import ObjectJob, ObjectJobInput
    from module.shared.common.errors.base import NotFoundError
    from module.memory_management.daily.repository.sqlite import DailyProductionRepository
    from module.memory_management.object.repository.sqlite import ObjectProductionRepository
    from module.memory_management.topic.repository.sqlite import TopicProductionRepository

    database, common = store
    daily = job("daily", target="same-target", status=Status.GENERATING)
    topic = job("topic", target="same-target", job_type=MemoryJobType.CREATE_TOPIC, status=Status.REVIEWING)
    obj = ObjectJob("object", MemoryJobType.EXTRACT_OBJECTS, "same-target", "dedupe:object", 10,
        Status.GENERATING, daily.available_at, ObjectJobInput("c", ("s",)), daily.created_at, daily.updated_at)
    values = {"daily": daily, "object": obj, "topic": topic}
    await save(database, *values.values())
    repo = {"daily": DailyProductionRepository, "object": ObjectProductionRepository,
            "topic": TopicProductionRepository}[family](database)
    assert (await repo.get_next_ready_job()).job_id == family
    assert await repo.recover_unfinished_attempts() == 1
    own = await repo.get_job(family)
    assert own.status == (Status.NEED_REVIEW if family == "topic" else Status.NEED_GENERATE)
    assert own.attempt_version == values[family].attempt_version + 1
    assert await repo.recover_unfinished_attempts() == 0
    for other_family, original in values.items():
        if other_family == family:
            continue
        assert await common.get_job(other_family) == original
        assert await repo.get_job(other_family) is None
        with pytest.raises(NotFoundError):
            await repo.fail_job(other_family, original.attempt_version, "wrong worker", expected_status=original.status)
        with pytest.raises(NotFoundError):
            await repo.retry_job(other_family, original.attempt_version, expected_status=original.status,
                available_at=datetime.now(timezone.utc), error="wrong worker")


@pytest.mark.asyncio
async def test_object_source_change_clears_saved_candidates(store):
    from module.memory_management.object.core.domain.job import ObjectJob, ObjectJobInput
    from module.memory_management.object.repository.sqlite import ObjectProductionRepository
    database, _ = store
    from module.memory_management.object.core.domain.document import MemoryObject, ObjectEvent
    from module.memory_management.object.core.domain.generation import ObjectSelectionNote
    from module.shared.memory.types.references import EvidenceRef
    base = job()
    evidence = EvidenceRef("s", "m", "决定休息", "t", 1, "hash")
    candidate = MemoryObject("obj", "c", "休息",
        (ObjectEvent("user", "决定休息", base.created_at.date(), None, (evidence,)),), "s:m", base.created_at)
    value = ObjectJob("object", MemoryJobType.EXTRACT_OBJECTS, "object:c:s", "object:c:s", 10,
        Status.NEED_INDEX, base.available_at, ObjectJobInput("c", ("s",)), base.created_at, base.updated_at,
        objects=(candidate,),
        selection_notes=(ObjectSelectionNote("休息", "keep", "有后续安排", ("m",)),))
    await save(database, value)
    repo = ObjectProductionRepository(database)
    assert await repo.retry_job(value.job_id, 0, expected_status=Status.NEED_INDEX,
        available_at=datetime.now(timezone.utc), error="changed", restart_generation=True)
    current = await repo.get_job(value.job_id)
    assert current.status == Status.NEED_GENERATE and current.objects == ()
    assert current.selection_notes == ()
