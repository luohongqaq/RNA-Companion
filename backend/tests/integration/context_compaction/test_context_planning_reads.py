"""上下文规划输入：真实 SQLite 的摘要版本和完整轮 token 统计读取。"""
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from module.shared.common.errors.base import InvalidStateError, NotFoundError
from module.character.core.domain.models import CharacterCard
from module.context_compaction.core.domain.errors import ContextSourceChangedError
from module.context_compaction.core.domain.references import CompactionMode
from module.shared.context.types.references import InheritedContextRef
from module.context_compaction.core.domain.job import CreateContextJobCommand, ContextJobType, ContextJobStatus
from module.context_compaction.core.domain.snapshots import ContextSnapshot, SnapShotState, SummarySourceManifest
from module.shared.context.types.summary import StructuredSummary, SummaryVersions
from module.context_compaction.core.domain.policy import ContextCompactionPolicy
from module.context_compaction.core.processing.service import ContextCompactionService
from module.context_compaction.repository.llm import ContextSummaryModelRepository
from module.chat.reply.core.domain.config.context_budget import ContextPromptBudget
from module.context_compaction.repository import mappers as context_mappers
from storage.sqlite.value_codecs import SQLiteValueCodec
from module.context_compaction.repository.sqlite import SQLiteContextMaintenanceRepository
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow, ContextSnapshotRow, ConversationRow, MessageRow, SessionRow, TurnRow


async def read_manifest(db, repo, plan):
    async with db.db_session() as session:
        return await repo._read_source_manifest(session, plan)


@pytest.mark.asyncio
async def test_begin_generate_only_registers_execution_and_rejects_duplicate_start(store):
    db, repo = store
    plan = await manifest_plan(db, repo)
    job_id = await repo.create_job(CreateContextJobCommand(
        ContextJobType.PREPARE_HANDOFF, "s", 1, 10, "begin-once"))
    started = await repo.begin_generate(job_id)
    assert started.status == ContextJobStatus.GENERATING
    assert started.attempt_version == started.attempt_count == 1
    assert started.source_manifest is None and started.plan is None
    assert (await repo.read_model_input(plan)).character_id == "c"
    assert await repo.get_job(job_id) == started
    assert await repo.begin_generate(job_id) is None


@pytest.mark.asyncio
async def test_restart_recovers_interrupted_generation_and_can_begin_new_attempt(store):
    db, repo = store
    plan = await manifest_plan(db, repo)
    job_id = await repo.create_job(CreateContextJobCommand(ContextJobType.PREPARE_HANDOFF, "s", 1, 10, "restart-generate"))
    interrupted = await repo.begin_generate(job_id)
    await db.close()
    reopened = SQLiteDatabase(db.path)
    try:
        await reopened.initialize()
        repository = SQLiteContextMaintenanceRepository(reopened)
        assert await repository.recover_unfinished_attempts() == 1
        recovered = await repository.get_job(job_id)
        assert recovered.status == ContextJobStatus.NEED_GENERATE
        assert recovered.attempt_version == interrupted.attempt_version + 1
        restarted = await repository.begin_generate(job_id)
        assert restarted.status == ContextJobStatus.GENERATING
        assert restarted.attempt_version > recovered.attempt_version
        assert restarted.attempt_count == interrupted.attempt_count + 1
        assert not await repository.fail_job(job_id, interrupted.attempt_version, "late failure", expected_status=ContextJobStatus.GENERATING)
        assert await repository.get_job(job_id) == restarted
    finally:
        await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [ContextJobStatus.NEED_VALIDATE, ContextJobStatus.NEED_PUBLISH])
async def test_restart_continues_saved_candidate_without_model_call(store, stage):
    db, repo = store
    plan = await manifest_plan(db, repo)
    job_id = await repo.create_job(CreateContextJobCommand(ContextJobType.PREPARE_HANDOFF, "s", 1, 10, f"restart-{stage}"))
    started = await repo.begin_generate(job_id)
    source = await repo.read_model_input(plan)

    class Counter:
        tokenizer_version = "restart-test"

        def count_messages(self, messages):
            return sum(len(message.content) for message in messages)

    counter = Counter()
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy())
    candidate = service.build_candidate_summary(
        summary=StructuredSummary("用户向我问候", source_refs=source.source_manifest.source_refs, keywords=("问候",)),
        manifest=source.source_manifest, attempt_version=started.attempt_version, candidate_version=1,
        versions=SummaryVersions(2, tokenizer_version=counter.tokenizer_version), token_counter=counter)
    assert await repo.save_candidate(job_id, started.attempt_version, candidate, source=source)
    if stage == ContextJobStatus.NEED_PUBLISH:
        validating = await repo.begin_validate(job_id)
        assert await repo.save_validation(job_id, validating.attempt_version, service.validate_candidate(source, candidate, counter))
    before = await repo.get_job(job_id)
    await db.close()
    reopened = SQLiteDatabase(db.path)
    try:
        await reopened.initialize()
        repository = SQLiteContextMaintenanceRepository(reopened)
        assert await repository.recover_unfinished_attempts() == 0
        assert await repository.get_job(job_id) == before
        # 不配置模型客户端或角色资源；已提交阶段直接续跑。
        resumed = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy(),
            repository=repository, token_counter=counter)
        if stage == ContextJobStatus.NEED_VALIDATE:
            await resumed.process_job(job_id)
            assert (await repository.get_job(job_id)).status == ContextJobStatus.NEED_PUBLISH
        await resumed.process_job(job_id)
        ready = await repository.get_job(job_id)
        assert ready.status == ContextJobStatus.READY and ready.candidate == candidate
        assert ready.attempt_version == started.attempt_version + 1 and ready.validation.passed
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_failed_source_read_keeps_registered_execution_for_recovery(store):
    db, repo = store
    plan = await manifest_plan(db, repo)
    job_id = await repo.create_job(CreateContextJobCommand(
        ContextJobType.PREPARE_HANDOFF, "s", 1, 10, "begin-rollback"))
    async with db.transaction() as session:
        (await session.get(TurnRow, "t1")).accepted_message_id = "u1"
    await repo.begin_generate(job_id)
    with pytest.raises(InvalidStateError):
        await repo.read_model_input(plan)
    job = await repo.get_job(job_id)
    assert job.status == ContextJobStatus.GENERATING
    assert job.attempt_version == 1 and job.source_manifest is None


async def manifest_plan(db, repo, upper=1):
    """准备真实的用户输入和正式回复，由现有规划器生成计划。"""
    for seq in range(1, upper + 1):
        await add_turn(db, seq)
        async with db.transaction() as session:
            session.add(MessageRow(message_id=f"u{seq}", turn_id=f"t{seq}", message_seq=0,
                role="user", content=f"question {seq}", created_at=datetime.now(timezone.utc)))
    return ContextCompactionService(prompt_budget=ContextPromptBudget(),
        compaction_policy=ContextCompactionPolicy()).plan_compaction(
            session_id="s", frozen_through_turn_seq=upper,
            turn_tokens=await repo.read_turn_token_infos("s", upper))


@pytest.mark.asyncio
async def test_manifest_hash_tracks_actual_text_and_revision_but_not_later_turns(store):
    db, repo = store
    plan = await manifest_plan(db, repo)
    first = await read_manifest(db, repo, plan)
    assert first == await read_manifest(db, repo, plan)
    assert first.source_refs[0].turn_id == "t1"
    assert first.source_refs[0].session_id == "s"
    await add_turn(db, 2)
    assert await read_manifest(db, repo, plan) == first
    async with db.transaction() as session:
        (await session.get(MessageRow, "u1")).content = "changed without revision"
    changed = await read_manifest(db, repo, plan)
    assert changed.source_hash != first.source_hash
    async with db.transaction() as session:
        (await session.get(TurnRow, "t1")).content_revision += 1
    revised = await read_manifest(db, repo, plan)
    assert revised.source_hash != changed.source_hash
    assert revised.source_refs[0].revision == 2


@pytest.mark.asyncio
async def test_manifest_ignores_unaccepted_reply_and_rejects_missing_formal_reply(store):
    db, repo = store
    plan = await manifest_plan(db, repo)
    first = await read_manifest(db, repo, plan)
    async with db.transaction() as session:
        session.add(MessageRow(message_id="unused", turn_id="t1", message_seq=2,
            role="character", content="not accepted", created_at=datetime.now(timezone.utc)))
    assert await read_manifest(db, repo, plan) == first
    async with db.transaction() as session:
        (await session.get(TurnRow, "t1")).accepted_message_id = "u1"
    with pytest.raises(InvalidStateError):
        await read_manifest(db, repo, plan)


@pytest.mark.asyncio
async def test_manifest_keeps_single_adopted_inherited_snapshot(store):
    from dataclasses import replace
    from module.shared.context.types.references import InheritedContextRef
    db, repo = store
    plan = await manifest_plan(db, repo)
    async with db.transaction() as session:
        (await session.get(SessionRow, "s")).session_seq = 2
        await session.flush()
        session.add(SessionRow(session_id="previous", conversation_id="conv", session_state="sealed",
                               session_start_time=datetime.now(timezone.utc)))
        await session.flush()
        (await session.get(SessionRow, "s")).predecessor_session_id = "previous"
        old = snapshot("fixed", SnapShotState.VALID)
        old = replace(old, session_id="previous", source_manifest=replace(old.source_manifest, session_id="previous"))
        session.add(context_mappers.snapshot_to_row(old))
        inherited = InheritedContextRef("previous", context_mappers.snapshot_to_ref(context_mappers.snapshot_to_row(old)), None, None)
        (await session.get(SessionRow, "s")).inherited_context_json = SQLiteValueCodec.inherited_context_to_json(inherited)
    plan = replace(plan, inherited_context=InheritedContextRef("previous", context_mappers.snapshot_to_ref(
        context_mappers.snapshot_to_row(old)), None, None))
    manifest = await read_manifest(db, repo, plan)
    assert manifest.inherited_context == plan.inherited_context
    assert manifest.inherited_context_included
    async with db.transaction() as session:
        (await session.get(ContextSnapshotRow, "fixed")).snapshot_state = "invalidated"
    with pytest.raises(InvalidStateError):
        await read_manifest(db, repo, plan)


async def save_model_input_job(repo, job_id, plan):
    started = await repo.begin_generate(job_id)
    source = await repo.read_model_input(plan)

    class Counter:
        tokenizer_version = "source-test"
        def count_messages(self, messages):
            return sum(len(message.content) for message in messages)

    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy())
    candidate = service.build_candidate_summary(summary=StructuredSummary("摘要", keywords=("聊天",)),
        manifest=source.source_manifest, attempt_version=started.attempt_version, candidate_version=1,
        versions=SummaryVersions(2, tokenizer_version="source-test"), token_counter=Counter())
    assert await repo.save_candidate(job_id, started.attempt_version, candidate, source=source)
    return await repo.get_job(job_id)


async def start_model_input_job(db, repo):
    plan = await manifest_plan(db, repo)
    job_id = await repo.create_job(CreateContextJobCommand(
        ContextJobType.PREPARE_HANDOFF, "s", 1, 10, "load-input"))
    return await save_model_input_job(repo, job_id, plan)


@pytest.mark.asyncio
async def test_process_job_runs_three_persisted_stages_with_injected_model_calls(store):
    """用假模型验证真实SQLite阶段闭环，不接具体SDK，也不在一次调用中循环重试。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    db, repo = store
    await manifest_plan(db, repo)
    job_id = await repo.create_job(CreateContextJobCommand(
        ContextJobType.PREPARE_HANDOFF, "s", 1, 10, "process-job"))

    class Counter:
        tokenizer_version = "test-counter"

        def count_messages(self, messages):
            return sum(len(message.content) for message in messages)

    summary = StructuredSummary("handoff summary", keywords=("handoff",))
    model = SimpleNamespace(generate=AsyncMock(return_value=summary))
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy(),
        repository=repo, token_counter=Counter(), model=ContextSummaryModelRepository(SimpleNamespace()), prompt_resources=SimpleNamespace())
    service.generate_summary = model.generate
    await service.process_job(job_id)
    assert (await repo.get_job(job_id)).status == ContextJobStatus.NEED_VALIDATE
    service._model = None
    await service.process_job(job_id)
    assert (await repo.get_job(job_id)).status == ContextJobStatus.NEED_PUBLISH
    await service.process_job(job_id)
    ready = await repo.get_job(job_id)
    assert ready.status == ContextJobStatus.READY and ready.published_snapshot is not None
    assert ready.candidate.summary == summary and ready.validation.passed
    model.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_model_json_retries_with_feedback_then_publishes_without_reviewer(store):
    import json
    from dataclasses import asdict
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    db, repo = store
    frozen_plan = await manifest_plan(db, repo)
    source = await repo.read_model_input(frozen_plan)
    job_id = await repo.create_job(CreateContextJobCommand(
        ContextJobType.PREPARE_HANDOFF, "s", 1, 10, "retry-json"))

    class Counter:
        tokenizer_version = "test-counter"

        def count_messages(self, messages):
            return sum(len(message.content) for message in messages)

    summary = StructuredSummary("用户问候", source_refs=source.source_manifest.source_refs, keywords=("问候",))
    responses = ["invalid JSON", json.dumps({"status": "ok", "summary": asdict(summary), "issues": []})]
    create = AsyncMock(side_effect=[SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
        message=SimpleNamespace(content=text))]) for text in responses])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    directory = Path(__file__).resolve().parents[3] / "resources/characters/character_deepseek"
    card = CharacterCard("c", "deepseek", (directory / "角色卡.md").read_text(encoding="utf-8-sig"), directory)
    resources = SimpleNamespace(load=AsyncMock(return_value=SimpleNamespace(character_card=card)))
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy(),
        repository=repo, token_counter=Counter(), model=ContextSummaryModelRepository(client), prompt_resources=resources)
    from module.context_compaction.core.scheduling.workflow import ContextWorkflow
    from module.shared.jobs.config.retry import RetryPolicy
    from datetime import timedelta
    workflow = ContextWorkflow(repo, service, retry_policy=RetryPolicy(delays=(timedelta(0),)))
    await workflow.execute(job_id)
    failed = await repo.get_job(job_id)
    assert failed.status == ContextJobStatus.NEED_GENERATE and failed.retry_count == 1 and failed.candidate is None
    await service.process_job(job_id)
    pending = await repo.get_job(job_id)
    assert pending.status == ContextJobStatus.NEED_VALIDATE and pending.attempt_version == 3
    payload = json.loads(create.await_args.kwargs["messages"][1]["content"])
    assert payload["previous_errors"] == failed.last_error
    assert payload["CHARACTER_CONTEXT"]["character_name"] == "deepseek"
    await service.process_job(job_id)
    await service.process_job(job_id)
    ready = await repo.get_job(job_id)
    assert ready.status == ContextJobStatus.READY and ready.validation.passed
    assert ready.candidate.summary.keywords == ("问候",)
    assert create.await_count == 2  # 首次失败和重生成；校验与发布不调用模型


@pytest.mark.asyncio
async def test_context_worker_polls_new_job_and_retries_validation_without_regeneration(store, monkeypatch):
    import asyncio
    import json
    from datetime import timedelta
    from dataclasses import asdict
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from module.context_compaction.core.scheduling.worker import ContextWorker
    from module.context_compaction.core.scheduling.workflow import ContextWorkflow
    from module.shared.jobs.config.retry import RetryPolicy

    db, repo = store
    plan = await manifest_plan(db, repo)
    source = await repo.read_model_input(plan)
    summary = StructuredSummary("用户向我问候", source_refs=source.source_manifest.source_refs, keywords=("问候",))
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
        message=SimpleNamespace(content=json.dumps({"status": "ok", "summary": asdict(summary), "issues": []})))]))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    directory = Path(__file__).resolve().parents[3] / "resources/characters/character_deepseek"
    card = CharacterCard("c", "deepseek", (directory / "角色卡.md").read_text(encoding="utf-8-sig"), directory)
    resources = SimpleNamespace(load=AsyncMock(return_value=SimpleNamespace(character_card=card)))

    class Counter:
        tokenizer_version = "worker-test"

        def count_messages(self, messages):
            return sum(len(message.content) for message in messages)

    published = asyncio.Event()
    original_validate, original_publish = repo.save_validation, repo.publish_ready_snapshot
    validation_calls = 0

    async def validate(*args):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 1:
            raise TimeoutError("transient validation storage error")
        return await original_validate(*args)

    async def publish(*args):
        result = await original_publish(*args)
        published.set()
        return result

    monkeypatch.setattr(repo, "save_validation", validate)
    monkeypatch.setattr(repo, "publish_ready_snapshot", publish)
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy(),
        repository=repo, token_counter=Counter(), model=ContextSummaryModelRepository(client), prompt_resources=resources)
    workflow = ContextWorkflow(repo, service,
        retry_policy=RetryPolicy(max_retries=2, delays=(timedelta(0),)))
    worker = ContextWorker(repo, workflow, poll_interval=0.01)
    await worker.start()
    try:
        # 不发送Event通知，轮询也必须能发现后登记的任务。
        job_id = await repo.create_job(CreateContextJobCommand(ContextJobType.PREPARE_HANDOFF, "s", 1, 10, "worker-loop"))
        await asyncio.wait_for(published.wait(), 5)
        ready = await repo.get_job(job_id)
        assert ready.status == ContextJobStatus.READY and ready.validation.passed
        assert ready.retry_count == 1 and ready.attempt_count == 1
        assert ready.candidate.summary == summary and validation_calls == 2
        create.assert_awaited_once()
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_read_fixed_inherited_context_never_derives_missing_reference(store):
    db, repo = store
    assert await repo.read_inherited_context("s") is None
    with pytest.raises(NotFoundError):
        await repo.read_inherited_context("missing")
    async with db.transaction() as session:
        (await session.get(SessionRow, "s")).session_seq = 2
        await session.flush()
        session.add(SessionRow(session_id="previous", conversation_id="conv", session_state="sealed",
            session_start_time=datetime.now(timezone.utc)))
        await session.flush()
        (await session.get(SessionRow, "s")).predecessor_session_id = "previous"
    with pytest.raises(InvalidStateError, match="固定继承"):
        await repo.read_inherited_context("s")
    ref = InheritedContextRef("previous", None, None, "fixed")
    async with db.transaction() as session:
        (await session.get(SessionRow, "s")).inherited_context_json = SQLiteValueCodec.inherited_context_to_json(ref)
    assert await repo.read_inherited_context("s") == ref


@pytest.mark.asyncio
async def test_load_model_input_uses_only_frozen_complete_turns_and_accepted_reply(store):
    db, repo = store
    job = await start_model_input_job(db, repo)
    await add_turn(db, 2)
    async with db.transaction() as session:
        session.add(MessageRow(message_id="unused_reply", turn_id="t1", message_seq=3,
            role="character", content="not accepted", created_at=datetime.now(timezone.utc)))
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy())
    source = await service._load_model_input(job=job, repository=repo)
    assert source.plan == job.plan and source.source_manifest == job.source_manifest
    assert source.inherited_context is None and source.context_only_turns == ()
    assert [turn.turn_seq for turn in source.owned_turns] == [1]
    assert source.owned_turns[0].user_messages[0].message_id == "u1"
    assert source.owned_turns[0].accepted_reply.message_id == "a1"
    assert await repo.get_job(job.job_id) == job


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["user_text", "reply_text", "revision", "accepted_reply", "unfinished"])
async def test_load_model_input_rejects_changed_source_even_without_revision_increment(store, change):
    db, repo = store
    job = await start_model_input_job(db, repo)
    async with db.transaction() as session:
        if change in ("user_text", "reply_text"):
            (await session.get(MessageRow, "u1" if change == "user_text" else "a1")).content = "edited"
        elif change == "revision":
            (await session.get(TurnRow, "t1")).content_revision += 1
        elif change == "unfinished":
            (await session.get(TurnRow, "t1")).turn_state = "current"
        else:
            session.add(MessageRow(message_id="new_reply", turn_id="t1", message_seq=2,
                role="character", content="replacement", created_at=datetime.now(timezone.utc)))
            await session.flush()
            (await session.get(TurnRow, "t1")).accepted_message_id = "new_reply"
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy())
    with pytest.raises(ContextSourceChangedError):
        await service._load_model_input(job=job, repository=repo)
    assert await repo.get_job(job.job_id) == job


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tail", "snapshot_text", "fixed_reference", "snapshot_state"])
async def test_load_model_input_includes_fixed_snapshot_and_inherited_tail(store, change):
    db, repo = store
    plan = await manifest_plan(db, repo)
    async with db.transaction() as session:
        (await session.get(SessionRow, "s")).session_seq = 2
        await session.flush()
        session.add(SessionRow(session_id="previous", conversation_id="conv", session_state="sealed",
            session_start_time=datetime.now(timezone.utc)))
        await session.flush()
        summary = snapshot("fixed", SnapShotState.VALID)
        summary = replace(summary, session_id="previous", source_manifest=replace(summary.source_manifest, session_id="previous"))
        session.add(context_mappers.snapshot_to_row(summary))
        session.add(TurnRow(turn_id="tail", session_id="previous", turn_seq=2, turn_state="complete",
            turn_token_count=10, cumulative_token_count=10, tokenizer_version="test"))
        await session.flush()
        session.add(MessageRow(message_id="tail_user", turn_id="tail", message_seq=0,
            role="user", content="previous question", created_at=datetime.now(timezone.utc)))
        session.add(MessageRow(message_id="tail_reply", turn_id="tail", message_seq=1,
            role="character", content="previous tail", created_at=datetime.now(timezone.utc)))
        await session.flush()
        (await session.get(TurnRow, "tail")).accepted_message_id = "tail_reply"
        inherited = InheritedContextRef.handoff("previous", 2, context_mappers.snapshot_to_ref(context_mappers.snapshot_to_row(summary)))
        current = await session.get(SessionRow, "s")
        current.predecessor_session_id = "previous"
        current.inherited_context_json = SQLiteValueCodec.inherited_context_to_json(inherited)
    plan = replace(plan, inherited_context=await repo.read_inherited_context("s"))
    job_id = await repo.create_job(CreateContextJobCommand(ContextJobType.PREPARE_HANDOFF, "s", 1, 10, "inherited-input"))
    job = await save_model_input_job(repo, job_id, plan)
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy())
    source = await service._load_model_input(job=job, repository=repo)
    assert source.inherited_context.ref == inherited
    assert source.inherited_context.snapshot.summary_text == "fixed"
    assert [turn.turn_id for turn in source.inherited_context.complete_turns] == ["tail"]
    assert [turn.turn_id for turn in source.owned_turns] == ["t1"]
    assert {ref.turn_id for ref in source.source_manifest.source_refs} == {"t1", "tail"}
    async with db.transaction() as session:
        if change == "tail":
            (await session.get(MessageRow, "tail_reply")).content = "edited tail"
        elif change == "snapshot_text":
            (await session.get(ContextSnapshotRow, "fixed")).rendered_summary_text = "edited snapshot"
        elif change == "snapshot_state":
            (await session.get(ContextSnapshotRow, "fixed")).snapshot_state = "invalidated"
        else:
            (await session.get(SessionRow, "s")).inherited_context_json = SQLiteValueCodec.inherited_context_to_json(
                replace(inherited, source_hash="changed"))
    with pytest.raises(ContextSourceChangedError):
        await service._load_model_input(job=job, repository=repo)


@pytest_asyncio.fixture
async def store(tmp_path):
    db = SQLiteDatabase(tmp_path / "planning_reads.db")
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


def snapshot(sid, state):
    manifest = SummarySourceManifest("s", None, (), "source-hash", None, False)
    return ContextSnapshot(sid, "s", state, 1, StructuredSummary(sid), sid, 3, sid, SummaryVersions(1), manifest, datetime.now(timezone.utc))


async def add_turn(db, seq, *, state="complete", count=10, cumulative=None):
    async with db.transaction() as session:
        turn = TurnRow(turn_id=f"t{seq}", session_id="s", turn_seq=seq, turn_state=state,
                       turn_token_count=count, cumulative_token_count=seq * 10 if cumulative is None else cumulative,
                       tokenizer_version="tokenizer-v1")
        session.add(turn)
        await session.flush()
        if state == "complete":
            session.add(MessageRow(message_id=f"a{seq}", turn_id=turn.turn_id, message_seq=1,
                                   role="character", content="回复", created_at=datetime.now(timezone.utc)))
            await session.flush()
            turn.accepted_message_id = f"a{seq}"


@pytest.mark.asyncio
async def test_snapshot_selects_valid_by_session_not_snapshot_id(store):
    db, repo = store
    expected = snapshot("snapshot-valid", SnapShotState.VALID)
    async with db.transaction() as session:
        session.add(context_mappers.snapshot_to_row(expected))
    assert await repo.read_current_snapshot("s") == expected
    # 封存后也能读取，供后继规划器准备继承材料。
    async with db.transaction() as session:
        (await session.get(SessionRow, "s")).session_state = "sealed"
    assert await repo.read_current_snapshot("s") == expected


@pytest.mark.asyncio
async def test_missing_snapshot_and_session_are_distinct(store):
    _, repo = store
    assert await repo.read_current_snapshot("s") is None
    assert await repo.read_turn_token_infos("s", 0) == ()
    with pytest.raises(NotFoundError):
        await repo.read_current_snapshot("missing")
    with pytest.raises(NotFoundError):
        await repo.read_turn_token_infos("missing", 0)


@pytest.mark.asyncio
async def test_legacy_snapshot_missing_provenance_is_not_fabricated(store):
    db, repo = store
    async with db.transaction() as session:
        row = context_mappers.snapshot_to_row(snapshot("legacy", SnapShotState.VALID))
        row.source_manifest_json = None
        session.add(row)
    with pytest.raises(ValueError, match="source manifest"):
        await repo.read_current_snapshot("s")


@pytest.mark.asyncio
async def test_token_prefix_is_ordered_and_excludes_current_tail(store):
    db, repo = store
    await add_turn(db, 2)
    await add_turn(db, 1)
    await add_turn(db, 3, state="current")
    infos = await repo.read_turn_token_infos("s", 2)
    assert [(i.turn_seq, i.token_count, i.cumulative_token_count) for i in infos] == [(1, 10, 10), (2, 10, 20)]
    assert all(i.tokenizer_version == "tokenizer-v1" and i.session_id == "s" for i in infos)
    with pytest.raises(InvalidStateError):
        await repo.read_turn_token_infos("s", 3)


@pytest.mark.asyncio
async def test_missing_middle_turn_is_not_silently_skipped(store):
    db, repo = store
    await add_turn(db, 1)
    await add_turn(db, 3)
    with pytest.raises(InvalidStateError):
        await repo.read_turn_token_infos("s", 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("turn_token_count", -1), ("cumulative_token_count", 99),
    ("tokenizer_version", None), ("tokenizer_version", " "),
    ("tokenizer_version", "other-version"), ("accepted_message_id", None),
])
async def test_unusable_statistics_are_rejected(store, field, value):
    db, repo = store
    await add_turn(db, 1)
    await add_turn(db, 2)
    async with db.transaction() as session:
        setattr(await session.get(TurnRow, "t2"), field, value)
    with pytest.raises(InvalidStateError):
        await repo.read_turn_token_infos("s", 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("upper", [-1, True, 1.5])
async def test_invalid_upper_bound(store, upper):
    _, repo = store
    with pytest.raises(InvalidStateError):
        await repo.read_turn_token_infos("s", upper)


@pytest.mark.asyncio
async def test_zero_token_turn_with_valid_statistics_is_allowed(store):
    db, repo = store
    await add_turn(db, 1, count=0, cumulative=0)
    assert (await repo.read_turn_token_infos("s", 1))[0].token_count == 0
