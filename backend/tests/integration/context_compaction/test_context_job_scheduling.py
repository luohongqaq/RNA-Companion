"""真实 SQLite：单协程任务选择、队首顺序与历史 payload 兼容。"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from module.context_compaction.core.domain.source import SummaryModelInput
from module.context_compaction.core.domain.job import ContextJob, ContextJobStatus as Status, ContextJobType
from module.shared.context.types.turns import TurnRange
from module.shared.common.errors.base import InvalidStateError, NotFoundError
from module.context_compaction.core.domain.policy import ContextCompactionPolicy
from module.context_compaction.core.processing.service import ContextCompactionService
from module.context_compaction.core.domain.plan import TurnTokenInfo
from module.context_compaction.core.domain.candidate import SummaryCandidate, SummaryValidation
from module.context_compaction.core.domain.snapshots import SummarySourceManifest
from module.shared.context.types.summary import StructuredSummary, SummaryVersions
from module.chat.reply.core.domain.config.context_budget import ContextPromptBudget
from module.context_compaction.repository import mappers as context_mappers
from module.context_compaction.repository.sqlite import SQLiteContextMaintenanceRepository
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow, ContextJobRow, ContextSnapshotRow, ConversationRow, SessionRow


def fixed_source():
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy())
    plan = service.plan_compaction(session_id="s", frozen_through_turn_seq=4,
        turn_tokens=tuple(TurnTokenInfo(f"t{i}", "s", i, 10, i * 10, "test") for i in range(1, 5)))
    # Intentionally independent of candidate fields so mismatched-candidate tests retain their checks.
    manifest = SummarySourceManifest("s", TurnRange("s", 1, 4), (), "source-hash", None, False)
    return SummaryModelInput(plan, manifest, ())


@pytest_asyncio.fixture
async def store(tmp_path):
    db = SQLiteDatabase(tmp_path / "ready.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    async with db.transaction() as session:
        session.add(CharacterRow(character_id="c", name="角色", character_setting="设定"))
        await session.flush()
        session.add(ConversationRow(conversation_id="conv", character_id="c", state="active", created_at=now, updated_at=now))
        await session.flush()
        session.add(SessionRow(session_id="s", conversation_id="conv", session_state="open", session_start_time=now))
        await session.flush()
        (await session.get(ConversationRow, "conv")).current_session_id = "s"
    try:
        yield db, SQLiteContextMaintenanceRepository(db)
    finally:
        await db.close()


async def add_job(db, jid="j1", *, status=Status.NEED_GENERATE, kind=ContextJobType.BUILD_SNAPSHOT,
                  available_at=None, created_at=None, validation=None):
    now = datetime.now(timezone.utc)
    job = ContextJob(jid, kind, "s", "session:s", jid, 1, status,
                    available_at or now, 4, created_at or now, now, validation=validation)
    async with db.transaction() as session:
        session.add(context_mappers.context_job_to_row(job))
    return job


async def prepare_running_job(db):
    """固定生成阶段产物，测试只关注候选保存，不调用模型。"""
    job = await add_job(db, status=Status.GENERATING, kind=ContextJobType.PREPARE_HANDOFF)
    service = ContextCompactionService(
        prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy(),
    )
    plan = service.plan_compaction(session_id="s", frozen_through_turn_seq=4,
        turn_tokens=tuple(TurnTokenInfo(f"t{i}", "s", i, 10, i * 10, "test") for i in range(1, 5)))
    manifest = SummarySourceManifest("s", TurnRange("s", 1, 4), (), "source-hash", None, False)
    job = replace(job, plan=plan, source_manifest=manifest, attempt_version=1, attempt_count=1)
    async with db.transaction() as session:
        (await session.get(ContextJobRow, job.job_id)).payload_json = context_mappers.context_job_payload_to_json(job)
    candidate = SummaryCandidate(1, manifest, 4, StructuredSummary("summary"), "summary", 5, "candidate-hash", SummaryVersions(1), datetime.now(timezone.utc), 1)
    return job, candidate


@pytest.mark.asyncio
async def test_save_candidate_persists_payload_and_advances_atomically(store):
    db, repo = store
    job, candidate = await prepare_running_job(db)
    assert await repo.save_candidate(job.job_id, 1, candidate, source=fixed_source()) is True
    saved = await repo.get_job(job.job_id)
    assert saved.status == Status.NEED_VALIDATE
    assert saved.candidate == candidate
    assert saved.plan == job.plan and saved.source_manifest == job.source_manifest
    assert saved.attempt_version == saved.attempt_count == 1
    async with db.db_session() as session:
        row = await session.get(ContextJobRow, job.job_id)
        assert row.status == "need_validate"
        assert row.payload_json["candidate"]["content_hash"] == "candidate-hash"
    assert await repo.save_candidate(job.job_id, 1, candidate, source=fixed_source()) is True
    assert await repo.get_job(job.job_id) == saved
    assert await repo.save_candidate(job.job_id, 1, replace(candidate, summary_text="changed"), source=fixed_source()) is False
    assert await repo.get_job(job.job_id) == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("argument_version,candidate_version", [(0, 1), (1, 0)])
async def test_save_candidate_rejects_stale_attempt_without_writing(store, argument_version, candidate_version):
    db, repo = store
    job, candidate = await prepare_running_job(db)
    assert await repo.save_candidate(job.job_id, argument_version,
        replace(candidate, attempt_version=candidate_version), source=fixed_source()) is False
    assert await repo.get_job(job.job_id) == job


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [Status.NEED_GENERATE, Status.FAILED, Status.OBSOLETE])
async def test_save_candidate_rejects_non_generating_tasks(store, status):
    db, repo = store
    job, candidate = await prepare_running_job(db)
    async with db.transaction() as session:
        (await session.get(ContextJobRow, job.job_id)).status = status.value
    assert await repo.save_candidate(job.job_id, 1, candidate, source=fixed_source()) is False
    assert (await repo.get_job(job.job_id)).candidate is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_source", [False, True])
async def test_save_candidate_rejects_mismatched_source_or_coverage(store, wrong_source):
    db, repo = store
    job, candidate = await prepare_running_job(db)
    candidate = (replace(candidate, source_manifest=replace(candidate.source_manifest, source_hash="other"))
                 if wrong_source else replace(candidate, covered_through_turn_seq=3))
    with pytest.raises(InvalidStateError):
        await repo.save_candidate(job.job_id, 1, candidate, source=fixed_source())
    assert await repo.get_job(job.job_id) == job


@pytest.mark.asyncio
async def test_save_candidate_missing_job_is_explicit(store):
    db, repo = store
    _, candidate = await prepare_running_job(db)
    with pytest.raises(NotFoundError):
        await repo.save_candidate("missing", 1, candidate, source=fixed_source())


@pytest.mark.asyncio
async def test_validation_is_saved_once_without_changing_candidate(store):
    db, repo = store
    job, candidate = await prepare_running_job(db)
    assert await repo.save_candidate(job.job_id, 1, candidate, source=fixed_source())
    check = SummaryValidation(1, True, (), "validation-v2", candidate.content_hash)
    assert await repo.begin_validate(job.job_id) is not None
    assert await repo.save_validation(job.job_id, 2, check)
    saved = await repo.get_job(job.job_id)
    assert saved.status == Status.NEED_PUBLISH and saved.candidate == candidate and saved.validation == check
    assert await repo.save_validation(job.job_id, 2, check)
    assert await repo.get_job(job.job_id) == saved
    assert not await repo.save_validation(job.job_id, 0, check)
    assert not await repo.save_validation(job.job_id, 2, replace(check, issues=("different",)))
    payload = context_mappers.context_job_payload_to_json(saved)
    assert not set(payload).intersection({"review", "precheck", "approved_candidate"})


@pytest.mark.asyncio
async def test_publish_ready_snapshot_uses_validated_candidate(store):
    db, repo = store
    job, candidate = await prepare_running_job(db)
    assert await repo.save_candidate(job.job_id, 1, candidate, source=fixed_source())
    check = SummaryValidation(1, True, (), "v2", candidate.content_hash)
    assert await repo.begin_validate(job.job_id) is not None
    assert await repo.save_validation(job.job_id, 2, check)
    ref = await repo.publish_ready_snapshot(job.job_id, 2)
    saved = await repo.get_job(job.job_id)
    assert saved.status == Status.READY and saved.finished_at is None
    assert saved.published_snapshot == ref
    assert await repo.save_validation(job.job_id, 2, check)
    async with db.db_session() as session:
        row = await session.get(ContextSnapshotRow, ref.snapshot_id)
        assert row.snapshot_state == "ready" and row.producer_job_id == job.job_id
        snapshot = context_mappers.snapshot_to_domain(row)
        assert snapshot.summary == candidate.summary and snapshot.content_hash == candidate.content_hash
        assert snapshot.token_count == candidate.token_count
        assert (await session.get(SessionRow, "s")).session_state == "open"
        assert (await session.get(ConversationRow, "conv")).current_session_id == "s"
        columns = {item[1] for item in (await session.execute(text("PRAGMA table_info(context_snapshots)"))).all()}
        assert not columns.intersection({"snapshot_version", "base_snapshot_json", "recursion_depth"})
    with pytest.raises(IntegrityError):
        async with db.transaction() as session:
            session.add(context_mappers.snapshot_to_row(replace(snapshot, snapshot_id="duplicate")))
            await session.flush()
    assert await repo.get_next_executable_job() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt_count,expected_status", [(1, Status.NEED_GENERATE), (2, Status.FAILED)])
async def test_failed_validation_retries_or_finishes(store, attempt_count, expected_status):
    db, repo = store
    job, candidate = await prepare_running_job(db)
    async with db.transaction() as session:
        (await session.get(ContextJobRow, job.job_id)).payload_json = context_mappers.context_job_payload_to_json(
            replace(job, attempt_count=attempt_count))
    assert await repo.save_candidate(job.job_id, 1, candidate, source=fixed_source())
    check = SummaryValidation(1, False, ("too long",), "v2", candidate.content_hash)
    assert await repo.begin_validate(job.job_id) is not None
    assert await repo.save_validation(job.job_id, 2, check)
    saved = await repo.get_job(job.job_id)
    assert saved.status == expected_status and saved.last_error == "too long"
    assert saved.retry_count == 1 and saved.candidate == candidate
    assert (saved.finished_at is not None) == (expected_status == Status.FAILED)
    assert await repo.save_validation(job.job_id, 2, check)
    assert await repo.get_job(job.job_id) == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["hash", "version", "over_limit"])
async def test_validation_rejects_wrong_binding_or_budget_atomically(store, failure):
    db, repo = store
    job, candidate = await prepare_running_job(db)
    if failure == "over_limit":
        candidate = replace(candidate, token_count=1000)
    assert await repo.save_candidate(job.job_id, 1, candidate, source=fixed_source())
    before = await repo.get_job(job.job_id)
    check = SummaryValidation(2 if failure == "version" else 1, True, (), "v2",
        "other" if failure == "hash" else candidate.content_hash)
    assert await repo.begin_validate(job.job_id) is not None
    before = await repo.get_job(job.job_id)
    with pytest.raises(InvalidStateError):
        await repo.save_validation(job.job_id, 2, check)
    assert await repo.get_job(job.job_id) == before


@pytest.mark.asyncio
async def test_generation_runtime_retry_rejects_late_result(store):
    db, repo = store
    job, _ = await prepare_running_job(db)
    assert not await repo.retry_job(job.job_id, 0, expected_status=Status.GENERATING, available_at=datetime.now(timezone.utc), error="invalid JSON")
    assert await repo.retry_job(job.job_id, 1, expected_status=Status.GENERATING, available_at=datetime.now(timezone.utc), error="invalid JSON")
    saved = await repo.get_job(job.job_id)
    assert saved.status == Status.NEED_GENERATE and saved.last_error == "invalid JSON" and saved.retry_count == 1
    assert not await repo.retry_job(job.job_id, 1, expected_status=Status.GENERATING, available_at=datetime.now(timezone.utc), error="late")


@pytest.mark.asyncio
async def test_selection_is_read_only_and_resumes_saved_stage(store):
    db, repo = store
    check = SummaryValidation(1, True, (), "v1", "hash")
    await add_job(db, status=Status.NEED_VALIDATE, validation=check)
    first = await repo.get_next_executable_job()
    assert first == await repo.get_next_executable_job()
    assert first == await repo.get_job("j1")
    assert first.status == Status.NEED_VALIDATE and first.validation == check
    assert first.attempt_version == 0 and first.attempt_count == 0
    assert await repo.get_job("missing") is None


@pytest.mark.asyncio
async def test_delayed_head_blocks_same_group_but_not_handoff(store):
    db, repo = store
    now = datetime.now(timezone.utc)
    await add_job(db, available_at=now + timedelta(hours=1), created_at=now)
    await add_job(db, "j2", created_at=now + timedelta(seconds=1))
    assert await repo.get_next_executable_job() is None
    await add_job(db, "handoff", kind=ContextJobType.PREPARE_HANDOFF)
    assert (await repo.get_next_executable_job()).job_id == "handoff"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [Status.DONE, Status.FAILED, Status.OBSOLETE])
async def test_terminal_head_unblocks_next_task(store, status):
    db, repo = store
    now = datetime.now(timezone.utc)
    await add_job(db, created_at=now)
    await add_job(db, "j2", created_at=now + timedelta(seconds=1))
    assert (await repo.get_next_executable_job()).job_id == "j1"
    async with db.transaction() as session:
        (await session.get(ContextJobRow, "j1")).status = status.value
    assert (await repo.get_next_executable_job()).job_id == "j2"


@pytest.mark.asyncio
async def test_priority_orders_groups_without_bypassing_head(store):
    db, repo = store
    now = datetime.now(timezone.utc)
    await add_job(db, created_at=now)
    await add_job(db, "j2", created_at=now + timedelta(seconds=1))
    await add_job(db, "handoff", kind=ContextJobType.PREPARE_HANDOFF)
    async with db.transaction() as session:
        (await session.get(ContextJobRow, "j1")).priority = 10
        (await session.get(ContextJobRow, "j2")).priority = -1
    assert (await repo.get_next_executable_job()).job_id == "handoff"


@pytest.mark.asyncio
async def test_equal_timestamps_have_stable_id_order(store):
    db, repo = store
    now = datetime.now(timezone.utc)
    await add_job(db, "b", created_at=now)
    await add_job(db, "a", created_at=now)
    assert (await repo.get_next_executable_job()).job_id == "a"


@pytest.mark.asyncio
async def test_empty_queue(store):
    _, repo = store
    assert await repo.get_next_executable_job() is None


@pytest.mark.asyncio
async def test_ready_waits_for_handoff_without_being_scheduled_or_bypassed(store):
    db, repo = store
    now = datetime.now(timezone.utc)
    await add_job(db, "prepared", status=Status.READY, created_at=now)
    await add_job(db, "next", created_at=now + timedelta(seconds=1))
    assert await repo.get_next_executable_job() is None
    ready = await repo.get_job("prepared")
    assert ready.status == Status.READY and ready.finished_at is None
    await add_job(db, "other_group", kind=ContextJobType.PREPARE_HANDOFF)
    assert (await repo.get_next_executable_job()).job_id == "other_group"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["v1", "v2", "minimal"])
async def test_old_review_payload_is_not_supported(store, legacy):
    db, repo = store
    await add_job(db)
    async with db.transaction() as session:
        row = await session.get(ContextJobRow, "j1")
        payload = dict(row.payload_json)
        payload["schema_version"] = 1 if legacy == "v1" else 2
        row.payload_json = {"frozen_through_turn_seq": 4} if legacy == "minimal" else payload
    with pytest.raises((ValueError, KeyError)):
        await repo.get_next_executable_job()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [Status.NEED_GENERATE, Status.GENERATING, Status.NEED_VALIDATE, Status.NEED_PUBLISH])
async def test_fail_job_finishes_current_stage_and_preserves_committed_materials(store, status):
    db, repo = store
    job, candidate = await prepare_running_job(db)
    check = SummaryValidation(1, True, (), "v2", candidate.content_hash)
    before = replace(job, status=status, candidate=candidate if status in (Status.NEED_VALIDATE, Status.NEED_PUBLISH) else None,
        validation=check if status == Status.NEED_PUBLISH else None, retry_count=2)
    async with db.transaction() as session:
        row = await session.get(ContextJobRow, job.job_id)
        row.status, row.retry_count = before.status.value, before.retry_count
        row.payload_json = context_mappers.context_job_payload_to_json(before)
    assert not await repo.fail_job(job.job_id, 0, "stale error", expected_status=status)
    assert await repo.get_job(job.job_id) == before
    assert await repo.fail_job(job.job_id, 1, "permanent error", expected_status=status)
    failed = await repo.get_job(job.job_id)
    assert failed.status == Status.FAILED and failed.last_error == "permanent error"
    assert failed.finished_at is not None and failed.finished_at == failed.updated_at
    assert failed.plan == before.plan and failed.source_manifest == before.source_manifest
    assert failed.candidate == before.candidate and failed.validation == before.validation
    assert failed.attempt_version == before.attempt_version and failed.attempt_count == before.attempt_count
    assert failed.retry_count == before.retry_count and failed.available_at == before.available_at
    assert await repo.fail_job(job.job_id, 1, "permanent error", expected_status=status)
    assert not await repo.fail_job(job.job_id, 1, "different error", expected_status=status)
    assert await repo.get_job(job.job_id) == failed
    assert not await repo.save_candidate(job.job_id, 1, candidate, source=fixed_source())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [Status.READY, Status.DONE, Status.OBSOLETE])
async def test_fail_job_cannot_overwrite_ready_or_terminal_result(store, status):
    db, repo = store
    before = await add_job(db, status=status)
    assert not await repo.fail_job(before.job_id, before.attempt_version, "late failure", expected_status=status)
    assert await repo.get_job(before.job_id) == before


@pytest.mark.asyncio
async def test_fail_job_missing_job_is_explicit(store):
    _, repo = store
    with pytest.raises(NotFoundError):
        await repo.fail_job("missing", 0, "failure", expected_status=Status.GENERATING)


@pytest.mark.asyncio
async def test_startup_recovery_only_resets_generating_and_invalidates_old_results(store):
    db, repo = store
    running, candidate = await prepare_running_job(db)
    check = SummaryValidation(1, True, (), "v2", candidate.content_hash)
    preserved = {}
    async with db.transaction() as session:
        for status in (Status.NEED_GENERATE, Status.NEED_VALIDATE, Status.NEED_PUBLISH,
                       Status.READY, Status.DONE, Status.FAILED, Status.OBSOLETE):
            jid = f"preserve-{status.value}"
            job = replace(running, job_id=jid, dedupe_key=jid, status=status,
                candidate=candidate if status != Status.NEED_GENERATE else None,
                validation=check if status not in (Status.NEED_GENERATE, Status.NEED_VALIDATE) else None,
                last_error="existing detail")
            preserved[jid] = job
            session.add(context_mappers.context_job_to_row(job))
        second = replace(running, job_id="another-running", dedupe_key="another-running",
            attempt_version=5, attempt_count=3, retry_count=2,
            available_at=datetime.now(timezone.utc) + timedelta(hours=1))
        session.add(context_mappers.context_job_to_row(second))
    started_at = datetime.now(timezone.utc)
    assert await repo.recover_unfinished_attempts() == 2
    for old in (running, second):
        recovered = await repo.get_job(old.job_id)
        assert recovered.status == Status.NEED_GENERATE
        assert recovered.attempt_version == old.attempt_version + 1
        assert recovered.attempt_count == old.attempt_count and recovered.retry_count == old.retry_count
        assert recovered.available_at == recovered.updated_at >= started_at
        assert recovered.finished_at is None and recovered.last_error
        assert recovered.plan == old.plan and recovered.source_manifest == old.source_manifest
    for jid, old in preserved.items():
        assert await repo.get_job(jid) == old
    assert not await repo.save_candidate(running.job_id, running.attempt_version, candidate, source=fixed_source())
    assert not await repo.save_validation(running.job_id, running.attempt_version, check)
    assert not await repo.fail_job(running.job_id, running.attempt_version, "late exception", expected_status=Status.GENERATING)
    before_second_recovery = await repo.get_job(running.job_id)
    assert await repo.recover_unfinished_attempts() == 0
    assert await repo.get_job(running.job_id) == before_second_recovery


@pytest.mark.asyncio
async def test_startup_recovery_empty_queue_is_a_noop(store):
    _, repo = store
    assert await repo.recover_unfinished_attempts() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [Status.NEED_GENERATE, Status.GENERATING, Status.NEED_VALIDATE, Status.NEED_PUBLISH])
async def test_runtime_retry_preserves_materials_and_reschedules_only_expected_phase(store, status):
    db, repo = store
    before, candidate = await prepare_running_job(db)
    check = SummaryValidation(1, True, (), "v2", candidate.content_hash)
    before = replace(before, status=status, candidate=candidate, validation=check)
    async with db.transaction() as session:
        row = await session.get(ContextJobRow, before.job_id)
        row.status, row.payload_json = status.value, context_mappers.context_job_payload_to_json(before)
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert not await repo.retry_job(before.job_id, 0, expected_status=status, available_at=retry_at, error="stale")
    assert not await repo.retry_job(before.job_id, 1, expected_status=Status.DONE, available_at=retry_at, error="wrong stage")
    assert await repo.retry_job(before.job_id, 1, expected_status=status, available_at=retry_at, error="timeout")
    saved = await repo.get_job(before.job_id)
    assert saved.status == (Status.NEED_GENERATE if status == Status.GENERATING else status)
    assert saved.attempt_version == 1 + (1 if status == Status.GENERATING else 0)
    assert saved.candidate == candidate and saved.validation == check and saved.plan == before.plan
    assert saved.retry_count == 1 and saved.available_at == retry_at and saved.last_error == "timeout"
    assert await repo.get_next_executable_job() is None


@pytest.mark.asyncio
async def test_source_change_restarts_generation_and_cannot_publish_old_candidate(store):
    db, repo = store
    before, candidate = await prepare_running_job(db)
    assert await repo.save_candidate(before.job_id, 1, candidate, source=fixed_source())
    assert await repo.retry_job(before.job_id, 1, expected_status=Status.NEED_VALIDATE,
        available_at=datetime.now(timezone.utc), error="source changed", restart_generation=True)
    saved = await repo.get_job(before.job_id)
    assert saved.status == Status.NEED_GENERATE and saved.attempt_version == 2
    assert not await repo.save_validation(before.job_id, 1, SummaryValidation(1, True, (), "v2", candidate.content_hash))


@pytest.mark.asyncio
async def test_validation_recovery_preserves_candidate_and_fences_old_execution(store):
    db, repo = store
    job, candidate = await prepare_running_job(db)
    assert await repo.save_candidate(job.job_id, 1, candidate, source=fixed_source())
    started = await repo.begin_validate(job.job_id)
    assert started.status == Status.VALIDATING and started.attempt_version == 2
    assert started.attempt_count == 1
    assert await repo.begin_validate(job.job_id) is None
    check = SummaryValidation(1, True, (), "v2", candidate.content_hash)
    assert await repo.recover_unfinished_attempts() == 1
    recovered = await repo.get_job(job.job_id)
    assert recovered.status == Status.NEED_VALIDATE and recovered.attempt_version == 3
    assert recovered.candidate == candidate and recovered.plan == started.plan
    assert recovered.attempt_count == 1 and recovered.retry_count == 0
    assert not await repo.save_validation(job.job_id, 2, check)
    resumed = await repo.begin_validate(job.job_id)
    assert resumed.attempt_version == 4 and resumed.candidate.attempt_version == 1
    assert await repo.save_validation(job.job_id, 4, check)
    assert await repo.publish_ready_snapshot(job.job_id, 2) is None
    ref = await repo.publish_ready_snapshot(job.job_id, 4)
    assert ref is not None
    assert await repo.publish_ready_snapshot(job.job_id, 4) == ref
    assert await repo.recover_unfinished_attempts() == 0


@pytest.mark.asyncio
async def test_validation_runtime_retry_uses_saved_candidate_and_retry_cas(store):
    db, repo = store
    job, candidate = await prepare_running_job(db)
    assert await repo.save_candidate(job.job_id, 1, candidate, source=fixed_source())
    started = await repo.begin_validate(job.job_id)
    now = datetime.now(timezone.utc)
    assert not await repo.retry_job(job.job_id, started.attempt_version,
        expected_status=Status.VALIDATING, expected_retry_count=1, available_at=now, error="timeout")
    assert await repo.retry_job(job.job_id, started.attempt_version,
        expected_status=Status.VALIDATING, expected_retry_count=0, available_at=now, error="timeout")
    retried = await repo.get_job(job.job_id)
    assert retried.status == Status.NEED_VALIDATE and retried.candidate == candidate
    assert retried.attempt_count == 1 and retried.attempt_version == 3
    assert not await repo.fail_job(job.job_id, 3, "late", expected_status=Status.NEED_VALIDATE,
                                   expected_retry_count=0)


@pytest.mark.asyncio
async def test_publish_without_validation_cannot_create_snapshot(store):
    db, repo = store
    job, candidate = await prepare_running_job(db)
    assert await repo.publish_ready_snapshot(job.job_id, 1) is None
    async with db.transaction() as session:
        row = await session.get(ContextJobRow, job.job_id)
        row.status = Status.NEED_PUBLISH.value
    with pytest.raises(InvalidStateError):
        await repo.publish_ready_snapshot(job.job_id, 1)
    with pytest.raises(NotFoundError):
        await repo.publish_ready_snapshot("missing", 0)
