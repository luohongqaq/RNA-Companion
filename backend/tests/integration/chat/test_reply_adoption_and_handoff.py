"""T3 集成测试：验证正式回复的真实 SQLite 提交结果。"""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from module.character.core.domain.models import CharacterReply
from module.shared.context.types.visual import Expression, Pose, VisualState
from module.chat.conversation.core.domain.contracts import SaveMessageInput
from module.shared.common.errors.base import InvalidStateError
from module.shared.context.types.references import InheritedContextRef
from module.context_compaction.repository import mappers as context_mappers
from module.shared.common.time import BackendTime
from module.memory_recall.agent.core.domain.audit import MemoryAudit
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from storage.sqlite.value_codecs import SQLiteValueCodec
from module.chat.conversation.repository import mappers as conversation_mappers
from module.chat.reply.core.domain.fingerprint import ReplyFingerprint
from module.chat.reply.core.domain.contracts import (
    GenerationFreezeTicket,
    ReplyAcceptanceTicket,
)
from module.chat.reply.core.domain.materials import (
    ChatContextManifest,
    FrozenPrompt,
    MemoryManifest,
    PromptMessage,
    ReplyCandidate,
)
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.reply.repository.sqlite import (
    SQLiteGenerationRepository,
)
from storage.sqlite.models import (
    ChatEventRow,
    CharacterRow,
    ContextJobRow,
    ContextSnapshotRow,
    DailyMemoryRow,
    GenerationAttemptRow,
    GenerationJobRow,
    MessageRow,
    SessionRow,
    TopicMemoryRow,
    TurnRow,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("inherited_tokens,tail_tokens,raw_tokens,expected_jobs", [
    (None, 0, 2399, 0), (None, 0, 2400, 1),
    (600, 0, 1799, 0), (600, 0, 1800, 1),
    (900, 0, 1499, 0), (900, 0, 1500, 1),
    (600, 200, 1599, 0), (600, 200, 1600, 1),
    (None, 0, 2849, 1), (None, 0, 2850, 1), (None, 0, 3500, 1),
    (600, 0, 2249, 1), (600, 0, 2250, 1),
    (600, 200, 2049, 1), (600, 200, 2050, 1),
])
async def test_handoff_registration_counts_actual_inherited_materials(
    prepared_database, inherited_tokens, tail_tokens, raw_tokens, expected_jobs,
):
    """测试T3（正式采纳回复）在判断“是否要登记交接任务、
    是否进入等待交接状态”时，必须把本窗口实际继承的上下文材料（前驱摘要 + 继承的尾部原文轮）的 token 一并计入窗口用量"""
    database, chat_repository, repository = prepared_database
    if inherited_tokens is not None:
        saved = await chat_repository.accept_message(make_message())
        async with database.transaction() as session:
            turn = await session.get(TurnRow, saved.turn_id)
            current = await session.get(SessionRow, turn.session_id)
            current.session_seq = 2
            await session.flush()
            session.add(SessionRow(
                session_id="sealed_predecessor", conversation_id=current.conversation_id,
                session_state="sealed", session_start_time=datetime.now(timezone.utc),
            ))
            await session.flush()
            current.predecessor_session_id = "sealed_predecessor"
            session.add(ContextSnapshotRow(
                snapshot_id="incoming_fixed", session_id="sealed_predecessor",
                covered_through_turn_seq=0, summary_json={}, rendered_summary_text="summary",
                content_hash="fixed-hash", snapshot_state="valid", created_at=datetime.now(timezone.utc),
                rendered_token_count=inherited_tokens,
            ))
            if tail_tokens:
                session.add(TurnRow(turn_id="previous_tail", session_id="sealed_predecessor",
                    turn_seq=1, turn_state="complete", turn_token_count=tail_tokens,
                    cumulative_token_count=tail_tokens, tokenizer_version="test-tokenizer"))
                await session.flush()
                session.add(MessageRow(message_id="previous_reply", turn_id="previous_tail",
                    message_seq=1, role="character", content="tail", created_at=datetime.now(timezone.utc)))
                await session.flush()
                (await session.get(TurnRow, "previous_tail")).accepted_message_id = "previous_reply"
            await session.flush()
            current.inherited_context_json = SQLiteValueCodec.inherited_context_to_json(InheritedContextRef.handoff(
                "sealed_predecessor", 1 if tail_tokens else 0,
                context_mappers.snapshot_to_ref(await session.get(ContextSnapshotRow, "incoming_fixed")),
            ))

    saved, ticket, _ = await prepare_candidate(chat_repository, repository)
    assert await repository.accept_reply(
        ticket, turn_token_count=raw_tokens, tokenizer_version="test-tokenizer",
    ) is not None
    assert await repository.accept_reply(
        ticket, turn_token_count=raw_tokens, tokenizer_version="test-tokenizer",
    ) is not None
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(ContextJobRow)) == expected_jobs
        turn = await session.get(TurnRow, saved.turn_id)
        current = await session.get(SessionRow, turn.session_id)
        assert current.session_progress_tokens == raw_tokens
        waiting = raw_tokens + (inherited_tokens or 0) + tail_tokens >= 2850
        assert current.session_state == ("waiting_handoff" if waiting else "open")
        assert current.session_end_time is None


@pytest.mark.asyncio
async def test_waiting_handoff_blocks_new_turn_but_allows_background_generation(prepared_database):
    """前台不能在旧窗口开新轮，但后台交接摘要的生成流程照常运转"""
    from module.context_compaction.core.domain.policy import ContextCompactionPolicy
    from module.context_compaction.core.processing.service import ContextCompactionService
    from module.context_compaction.core.domain.job import ContextJobStatus
    from module.chat.reply.core.domain.config.context_budget import ContextPromptBudget
    from module.context_compaction.repository.sqlite import SQLiteContextMaintenanceRepository

    database, chat_repository, repository = prepared_database
    saved, ticket, _ = await prepare_candidate(chat_repository, repository)
    assert await repository.accept_reply(ticket, turn_token_count=3500, tokenizer_version="test-tokenizer")
    with pytest.raises(InvalidStateError, match="等待交接"):
        await chat_repository.accept_message(replace(make_message(), request_id="next_input"))
    # 已提交请求仍可幂等重放，等待状态不影响保存回执的查询。
    assert await chat_repository.accept_message(make_message()) == saved
    async with database.db_session() as session:
        turn = await session.get(TurnRow, saved.turn_id)
        assert turn.turn_state == "complete"
        assert await session.scalar(select(func.count()).select_from(MessageRow)) == 2
        assert await session.scalar(select(func.count()).select_from(TurnRow)) == 1

    context_repository = SQLiteContextMaintenanceRepository(database)
    job = await context_repository.get_next_executable_job()
    stats = await context_repository.read_turn_token_infos(job.session_id, job.frozen_through_turn_seq)
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy())
    plan = service.plan_compaction(session_id=job.session_id,
        frozen_through_turn_seq=job.frozen_through_turn_seq, turn_tokens=stats)
    started = await context_repository.begin_generate(job.job_id)
    assert started.status == ContextJobStatus.GENERATING
    async with database.db_session() as session:
        assert (await session.get(SessionRow, job.session_id)).session_state == "waiting_handoff"
        # 等待窗口仍占据当前窗口的位置，不能另建一个open窗口绕过交接。
        with pytest.raises(IntegrityError):
            async with database.transaction() as writer:
                writer.add(SessionRow(session_id="bypass", conversation_id="conversation_001",
                    session_state="open", session_start_time=datetime.now(timezone.utc)))
                await writer.flush()
        assert await session.scalar(text("PRAGMA foreign_keys")) == 1


@pytest.mark.asyncio
async def test_baseline_reinitialize_preserves_waiting_session_and_turns(prepared_database):
    """重复初始化保留等待交接的路由、原文和外键。"""
    database, chat_repository, repository = prepared_database
    saved, ticket, _ = await prepare_candidate(chat_repository, repository)
    accepted = await repository.accept_reply(ticket, turn_token_count=20, tokenizer_version="test-tokenizer")
    async with database.transaction() as session:
        turn = await session.get(TurnRow, saved.turn_id)
        current = await session.get(SessionRow, turn.session_id)
        current.session_state = "waiting_handoff"
    await database.initialize()
    await database.initialize()
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MessageRow)) == 2
        assert (await session.get(MessageRow, accepted.message_id)).turn_id == saved.turn_id
        assert not (await session.execute(text("PRAGMA foreign_key_check"))).all()
        assert await session.scalar(text("PRAGMA foreign_keys")) == 1
        assert list((await session.scalars(text("SELECT version FROM schema_migrations ORDER BY version"))).all()) == [1, 2, 3, 4, 5, 6, 7]
        assert (await session.get(SessionRow, current.session_id)).session_state == "waiting_handoff"
    assert await chat_repository.accept_message(make_message()) == saved


async def prepare_checked_handoff(prepared_database):
    """真实完成首轮并固定80%来源，再保存通过检查的摘要，尚未发布。"""
    from module.context_compaction.core.domain.policy import ContextCompactionPolicy
    from module.context_compaction.core.processing.service import ContextCompactionService
    from module.context_compaction.core.domain.candidate import SummaryCandidate, SummaryValidation
    from module.shared.context.types.summary import StructuredSummary, SummaryVersions
    from module.chat.reply.core.domain.config.context_budget import ContextPromptBudget
    from module.context_compaction.repository.sqlite import SQLiteContextMaintenanceRepository

    database, chat, generation = prepared_database
    first, ticket, _ = await prepare_candidate(chat, generation)
    assert await generation.accept_reply(ticket, turn_token_count=2400, tokenizer_version="test-tokenizer")
    context = SQLiteContextMaintenanceRepository(database)
    job = await context.get_next_executable_job()
    stats = await context.read_turn_token_infos(job.session_id, job.frozen_through_turn_seq)
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy())
    plan = service.plan_compaction(session_id=job.session_id, frozen_through_turn_seq=job.frozen_through_turn_seq,
        turn_tokens=stats)
    started = await context.begin_generate(job.job_id)
    source = await context.read_model_input(plan)
    candidate = SummaryCandidate(1, source.source_manifest, 1, StructuredSummary("handoff"), "handoff", 600,
        "handoff-hash", SummaryVersions(1, tokenizer_version="test-tokenizer"), datetime.now(timezone.utc),
        started.attempt_version)
    source = await context.read_model_input(plan)
    assert await context.save_candidate(job.job_id, started.attempt_version, candidate, source=source)
    check = SummaryValidation(1, True, (), "v1", candidate.content_hash)
    validating = await context.begin_validate(job.job_id)
    assert await context.save_validation(job.job_id, validating.attempt_version, check)
    return context, await context.get_job(job.job_id), first


@pytest.mark.asyncio
@pytest.mark.parametrize("ready_first", [True, False])
async def test_both_handoff_triggers_preserve_tail_and_route_next_input(prepared_database, ready_first):
    from module.chat.reply.core.processing.context_loader import ContextLoader
    from module.chat.reply.repository.context_sqlite import SQLiteContextReadRepository

    database, chat, generation = prepared_database
    context, job, _ = await prepare_checked_handoff(prepared_database)
    if ready_first:
        ref = await context.publish_ready_snapshot(job.job_id, job.attempt_version)
        assert (await context.get_job(job.job_id)).status.value == "ready"
    second, ticket, _ = await prepare_candidate(chat, generation,
        command=replace(make_message(), request_id="second", content="second turn"))
    assert await generation.accept_reply(ticket, turn_token_count=450, tokenizer_version="test-tokenizer")
    if not ready_first:
        async with database.db_session() as session:
            assert (await session.get(SessionRow, job.session_id)).session_state == "waiting_handoff"
        ref = await context.publish_ready_snapshot(job.job_id, job.attempt_version)
    done = await context.get_job(job.job_id)
    assert done.status.value == "done" and done.finished_at is not None
    async with database.db_session() as session:
        old = await session.get(SessionRow, job.session_id)
        successor = await session.scalar(select(SessionRow).where(SessionRow.auto_successor_of == old.session_id))
        assert old.session_state == "sealed" and old.session_end_time is not None
        assert successor.session_state == "open" and successor.session_progress_tokens == 0
        assert old.session_seq == 1 and successor.session_seq == 2
        assert (await session.get(ContextSnapshotRow, ref.snapshot_id)).snapshot_state == "valid"
        inherited = conversation_mappers.session_to_domain(successor).inherited_context
        assert inherited.summary_snapshot_ref == ref and inherited.source_hash
        assert inherited.source_range.from_seq == inherited.source_range.through_seq == 2
        assert (await session.get(TurnRow, second.turn_id)).session_id == old.session_id
        successor_id = successor.session_id
    # 重复交接只返回同一个后继，不创建第三个session。
    repeated = await context.commit_handoff(job.session_id)
    assert repeated.session_id == successor_id
    assert repeated.session_seq == 2
    next_input = await chat.accept_message(replace(make_message(), request_id="third", content="next window"))
    next_ticket = await generation.claim_job(next_input.job_id)
    boundary = next_ticket.input_boundary
    assert boundary.session_id == successor_id
    async with database.db_session() as session:
        assert (await session.get(TurnRow, boundary.current_turn.turn_id)).turn_seq == 1
    assert boundary.inherited_context == inherited
    loader = ContextLoader(SQLiteContextReadRepository(database))
    loaded = await loader.load(working_snapshot_ref=boundary.working_snapshot,
        complete_turns=boundary.complete_turns, current_turn_ref=boundary.current_turn,
        inherited_context_ref=boundary.inherited_context)
    assert loaded.inherited_context.snapshot.summary_text == "handoff"
    assert [turn.turn_seq for turn in loaded.inherited_context.complete_turns] == [2]


@pytest.mark.asyncio
async def test_handoff_failure_rolls_back_publication_and_session_route(prepared_database, monkeypatch):
    database, chat, generation = prepared_database
    context, job, _ = await prepare_checked_handoff(prepared_database)
    _, ticket, _ = await prepare_candidate(chat, generation,
        command=replace(make_message(), request_id="second", content="second turn"))
    assert await generation.accept_reply(ticket, turn_token_count=450, tokenizer_version="test-tokenizer")
    original_mapper = conversation_mappers.session_to_row

    def fail_successor(*args, **kwargs):
        raise RuntimeError("simulated insert failure")

    monkeypatch.setattr(conversation_mappers, "session_to_row", fail_successor)
    with pytest.raises(RuntimeError, match="simulated"):
        await context.publish_ready_snapshot(job.job_id, job.attempt_version)
    async with database.db_session() as session:
        old = await session.get(SessionRow, job.session_id)
        assert old.session_state == "waiting_handoff" and old.session_end_time is None
        assert await session.scalar(select(func.count()).select_from(ContextSnapshotRow)) == 0
        assert (await session.scalar(select(ContextJobRow))).status == "need_publish"
    monkeypatch.setattr(conversation_mappers, "session_to_row", original_mapper)
    await context.publish_ready_snapshot(job.job_id, job.attempt_version)
    assert (await context.get_job(job.job_id)).status.value == "done"


@pytest.mark.asyncio
async def test_oversized_inherited_tail_keeps_waiting_without_dropping_messages(prepared_database):
    database, chat, generation = prepared_database
    context, job, _ = await prepare_checked_handoff(prepared_database)
    _, ticket, _ = await prepare_candidate(chat, generation,
        command=replace(make_message(), request_id="second", content="long turn"))
    assert await generation.accept_reply(ticket, turn_token_count=2500, tokenizer_version="test-tokenizer")
    await context.publish_ready_snapshot(job.job_id, job.attempt_version)
    assert (await context.get_job(job.job_id)).status.value == "ready"
    async with database.db_session() as session:
        assert (await session.get(SessionRow, job.session_id)).session_state == "waiting_handoff"
        assert await session.scalar(select(func.count()).select_from(SessionRow)) == 1
        assert await session.scalar(select(func.count()).select_from(MessageRow)) == 4


@pytest_asyncio.fixture
async def prepared_database(tmp_path):
    """每条测试使用独立的临时数据库。"""
    database = SQLiteDatabase(tmp_path / "t3.sqlite3")

    try:
        # 使用真实迁移建表。
        await database.initialize()

        async with database.transaction() as session:
            session.add(
                CharacterRow(
                    character_id="character_test",
                    name="测试角色",
                    character_setting="你是一个桌宠。",
                )
            )

        chat_repository = SQLiteChatRepository(database)
        generation_repository = SQLiteGenerationRepository(database)

        yield database, chat_repository, generation_repository

    finally:
        await database.close()


def make_message():
    return SaveMessageInput(
        request_id="request_001",
        conversation_id="conversation_001",
        character_id="character_test",
        content="今天有点累",
    )


async def prepare_candidate(chat_repository, generation_repository, *, command=None, memory_manifest=None):
    """准备候选，不执行 T3，也不调用真实模型。"""

    # 1. 执行真实 T1，保存用户消息。
    command = make_message() if command is None else command
    saved = await chat_repository.accept_message(command)

    # 2. 执行 T2-A，领取任务。
    started = await generation_repository.claim_job(saved.job_id)
    assert started is not None

    boundary = started.input_boundary
    summary = "记忆摘要" if memory_manifest is not None else ""
    summary_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest() if summary else ""

    # 3. 准备与 T2-A 边界一致的最小提示词。
    prompt = FrozenPrompt(
        messages=(
            PromptMessage("system", f"你是一个桌宠。{summary}"),
            PromptMessage("user", command.content),
        ),
        token_count=None,
        context_manifest=ChatContextManifest(
            working_snapshot=boundary.working_snapshot,
            complete_turns=boundary.complete_turns,
            current_turn=boundary.current_turn,
            inherited_context=boundary.inherited_context,
        ),
        memory_manifest=memory_manifest or MemoryManifest(),
        character_card_version="test-card",
        tokenizer_version="unconfigured",
        memory_summary=summary,
        memory_summary_hash=summary_hash,
    )

    # 4. 执行 T2-B，将提示词真实提交到数据库。
    freeze_ticket = GenerationFreezeTicket(
        job_id=saved.job_id,
        attempt_id=started.attempt_id,
        turn_id=saved.turn_id,
        session_id=boundary.session_id,
        input_version=started.input_version,
        active_token=started.active_token,
        frozen_prompt=prompt,
        memory_audit=MemoryAudit(
            attempt_id=started.attempt_id, status="ready",
            source_refs=(*memory_manifest.daily, *memory_manifest.topic, *memory_manifest.other),
            source_cards=(), summary_hash=summary_hash, summary_status="ready", reason_code="",
            unresolved=(), skill_hash="", summary_prompt_hash="", character_view_hash="",
            total_seconds=0, trajectory=(),
        ) if memory_manifest is not None else None,
    )

    assert await generation_repository.freeze_prompt(
        freeze_ticket
    ) is True

    # 5. 构造固定回复，代替真实模型输出。
    reply = CharacterReply(
        response_zh="那就先休息一下吧。",
        visual=VisualState(
            pose_name=Pose("pose_001", "standing"),
            expression_name=Expression("expression_001", "smile"),
        ),
    )

    candidate = ReplyCandidate(
        reply=reply,
        candidate_hash=ReplyFingerprint.candidate_hash(reply),
        prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages),
        usage=None,
        created_at=datetime.now(timezone.utc),
    )

    ticket = ReplyAcceptanceTicket(
        attempt_id=started.attempt_id,
        input_version=started.input_version,
        active_token=started.active_token,
        prompt_hash=candidate.prompt_hash,
        candidate_hash=candidate.candidate_hash,
    )

    # 6. 真实保存候选，任务进入 need_accept。
    assert await generation_repository.save_candidate(
        ticket, candidate
    ) is True

    return saved, ticket, candidate


@pytest.mark.asyncio
async def test_reply_adoption_renews_only_frozen_formal_memory_versions(prepared_database):
    database, chat, repository = prepared_database
    now = datetime.now(timezone.utc)
    old_day = date(2026, 1, 1)
    future_day = BackendTime.local(now).date() + timedelta(days=3)
    async with database.transaction() as session:
        session.add(CharacterRow(character_id="other", name="其他角色", character_setting="测试"))
        await session.flush()
        daily = DailyMemoryRow(
            daily_id="daily", character_id="character_test", memory_date=old_day,
            source_session_ids_json=[], document_json={}, revision=2, content_hash="fixed",
            vector_document_key="daily-key", recall_tier="cold", last_mentioned_at=old_day,
            created_at=now, updated_at=now, published_at=now,
        )
        session.add(daily)
        for identifier, owner, revision, content_hash, mentioned, published in (
            ("topic", "character_test", 2, "fixed", None, now),
            ("changed-revision", "character_test", 3, "fixed", old_day, now),
            ("changed-content", "character_test", 2, "new", old_day, now),
            ("other-role", "other", 2, "fixed", old_day, now),
            ("unpublished", "character_test", 2, "fixed", old_day, None),
            ("future", "character_test", 2, "fixed", future_day, now),
        ):
            session.add(TopicMemoryRow(
                topic_id=identifier, character_id=owner, revision=revision, content_hash=content_hash,
                document_json={}, vector_document_key=identifier, recall_tier="hot",
                last_mentioned_at=mentioned, created_at=now, updated_at=now, published_at=published,
            ))
    daily_ref = MemoryRevisionRef(MemoryKind.DAILY, "daily", 2, "fixed")
    topics = tuple(MemoryRevisionRef(MemoryKind.TOPIC, key, 2, "fixed") for key in (
        "topic", "changed-revision", "changed-content", "other-role", "unpublished", "future", "missing",
    ))
    _, ticket, _ = await prepare_candidate(
        chat, repository, memory_manifest=MemoryManifest(daily=(daily_ref, daily_ref), topic=topics),
    )

    async with database.transaction() as session:
        await session.execute(text(
            "CREATE TRIGGER reject_reply_event BEFORE INSERT ON chat_events "
            "WHEN NEW.event_type = 'reply_committed' BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
        ))
    with pytest.raises(IntegrityError, match="test rollback"):
        await repository.accept_reply(ticket, turn_token_count=1, tokenizer_version="test")
    async with database.transaction() as session:
        assert (await session.get(DailyMemoryRow, "daily")).last_mentioned_at == old_day
        assert (await session.get(TopicMemoryRow, "topic")).last_mentioned_at is None
        await session.execute(text("DROP TRIGGER reject_reply_event"))

    accepted = await repository.accept_reply(ticket, turn_token_count=1, tokenizer_version="test")
    assert accepted is not None
    used_on = BackendTime.local(accepted.created_at).date()
    async with database.transaction() as session:
        daily = await session.get(DailyMemoryRow, "daily")
        assert (daily.last_mentioned_at, daily.recall_tier, daily.updated_at) == (used_on, "cold", now)
        assert (await session.get(TopicMemoryRow, "topic")).last_mentioned_at == used_on
        for key in ("changed-revision", "changed-content", "other-role", "unpublished"):
            assert (await session.get(TopicMemoryRow, key)).last_mentioned_at == old_day
        assert (await session.get(TopicMemoryRow, "future")).last_mentioned_at == future_day
        daily.last_mentioned_at = old_day

    assert await repository.accept_reply(ticket, turn_token_count=1, tokenizer_version="test") == accepted
    async with database.db_session() as session:
        assert (await session.get(DailyMemoryRow, "daily")).last_mentioned_at == old_day


@pytest.mark.asyncio
@pytest.mark.parametrize("material", ["candidate", "prompt", "bad_candidate", "bad_prompt"])
async def test_manual_retry_resumes_expensive_checkpoint(prepared_database, material):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from module.chat.reply.core.domain.models import GenerationFailure, GenerationFailureKind
    from module.shared.jobs.config.retry import RetryPolicy
    from module.chat.reply.core.scheduling.workflow import GenerationWorkflow
    from module.chat.reply.core.processing.completion import GenerationCompletionService

    database, chat, repository = prepared_database
    saved, old_ticket, candidate = await prepare_candidate(chat, repository)
    if material == "prompt":
        async with database.transaction() as session:
            attempt = await session.get(GenerationAttemptRow, old_ticket.attempt_id)
            attempt.candidate_json = None
            (await session.get(GenerationJobRow, saved.job_id)).state = "need_generate"
    failure = GenerationFailure(GenerationFailureKind.STORAGE, "test_failure", "失败", True,
                                datetime.now(timezone.utc))
    assert await repository.record_failure(saved.job_id, old_ticket.attempt_id, failure, RetryPolicy(max_retries=0))
    if material in ("bad_candidate", "bad_prompt"):
        async with database.transaction() as session:
            attempt = await session.get(GenerationAttemptRow, old_ticket.attempt_id)
            if material == "bad_prompt":
                attempt.prompt_hash = "damaged"
            else:
                attempt.candidate_json = {**attempt.candidate_json, "candidate_hash": "damaged"}
    resumed = await repository.retry_failed_job(saved.job_id)
    if material == "bad_prompt":
        assert resumed.status.value == "need_prepare"
        assert resumed.current_attempt_id is None
        return
    assert resumed.status.value == ("need_accept" if material == "candidate" else "need_generate")
    new_attempt = await repository.get_attempt(resumed.current_attempt_id)
    assert new_attempt.attempt_id != old_ticket.attempt_id
    assert new_attempt.active_token > old_ticket.active_token
    assert new_attempt.frozen_prompt is not None
    assert new_attempt.failure is None and new_attempt.finished_at is None
    assert (await repository.retry_failed_job(saved.job_id)).current_attempt_id == new_attempt.attempt_id
    # 旧票据不能采用，恢复后的有效材料由新 attempt 持有。
    assert await repository.accept_reply(old_ticket, turn_token_count=10, tokenizer_version="test") is None
    reply_model = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(candidate=candidate)))
    counter = SimpleNamespace(count_messages=lambda messages: 10, tokenizer_version="test")
    preparation = SimpleNamespace(load_context=AsyncMock(), assemble=AsyncMock())
    completion = GenerationCompletionService(repository, reply_model, counter)
    workflow = GenerationWorkflow(repository, preparation, completion)
    assert await workflow.execute(saved.job_id)
    preparation.load_context.assert_not_awaited()
    preparation.assemble.assert_not_awaited()
    assert reply_model.generate.await_count == (0 if material == "candidate" else 1)
    assert (await repository.get_job(saved.job_id)).status.value == "done"
    assert not await workflow.execute(saved.job_id)
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MessageRow)) == 2
        assert await session.scalar(select(func.count()).select_from(GenerationJobRow)) == 1


@pytest.mark.asyncio
async def test_accept_reply_creates_formal_message(prepared_database):
    """验证正式回复的SQLite 提交结果。"""
    # 1. 准备真实数据库及持久候选。
    database, chat_repository, repository = prepared_database
    saved, ticket, candidate = await prepare_candidate(
        chat_repository, repository
    )

    # 2. 执行真实 T3。
    result = await repository.accept_reply(
        ticket,
        turn_token_count=20,
        tokenizer_version="test-tokenizer",
    )

    # 3. 检查返回的正式回复。
    assert result is not None
    assert result.conversation_id == "conversation_001"
    assert result.turn_id == saved.turn_id
    assert result.content == "那就先休息一下吧。"
    assert result.visual == candidate.reply.visual

    events = await chat_repository.list_events_after(0)
    assert len(events) == 1
    assert events[0].event_id > 0
    assert events[0].job_id == saved.job_id
    assert events[0].reply == result
    assert events[0].created_at == result.created_at

    # 4. 新建查询 Session，检查真正提交后的数据。
    async with database.db_session() as session:
        # 正式消息正确保存。
        message = await session.get(MessageRow, result.message_id)
        assert message is not None
        assert message.turn_id == saved.turn_id
        assert message.role == "character"
        assert message.content == "那就先休息一下吧。"
        assert message.message_seq == 2

        assert message.visual_json == {
            "pose_name": {
                "pose_id": "pose_001",
                "pose_name": "standing",
            },
            "expression_name": {
                "expression_id": "expression_001",
                "expression_name": "smile",
            },
        }

        # 原始用户消息仍然保留。
        user_message = await session.get(MessageRow, saved.message_id)
        assert user_message.content == "今天有点累"
        assert user_message.role == "user"
        assert user_message.message_seq == 1

        # 当前轮封口，并指向正式消息。
        turn = await session.get(TurnRow, saved.turn_id)
        assert turn.turn_state == "complete"
        assert turn.accepted_message_id == result.message_id
        assert turn.turn_token_count == 20
        assert turn.cumulative_token_count == 20
        assert turn.tokenizer_version == "test-tokenizer"

        # attempt 和 job 同步完成。
        attempt = await session.get(
            GenerationAttemptRow, ticket.attempt_id
        )
        assert attempt.attempt_state == "accepted"
        assert attempt.completed_at is not None

        job = await session.get(GenerationJobRow, saved.job_id)
        assert job.state == "done"
        assert job.finished_at is not None

        # session 累计本轮 token。
        chat_session = await session.get(SessionRow, turn.session_id)
        assert chat_session.session_progress_tokens == 20

        # 一条用户消息，一条正式回复。
        message_count = await session.scalar(
            select(func.count()).select_from(MessageRow)
        )
        assert message_count == 2


@pytest.mark.asyncio
async def test_repeated_adoption_does_not_duplicate_records(
    prepared_database,
):
    # 1. 准备。
    database, chat_repository, repository = prepared_database
    saved, ticket, candidate = await prepare_candidate(
        chat_repository, repository
    )

    # 2. 第一次采用。
    first = await repository.accept_reply(
        ticket,
        turn_token_count=20,
        tokenizer_version="test-tokenizer",
    )
    assert first is not None

    async with database.db_session() as session:
        turn = await session.get(TurnRow, saved.turn_id)
        first_row_version = turn.row_version

    # 3. 同一候选再次采用。
    second = await repository.accept_reply(
        ticket,
        turn_token_count=20,
        tokenizer_version="test-tokenizer",
    )

    # 4. 返回原正式回复。
    assert second is not None
    assert second.message_id == first.message_id
    assert second.content == first.content
    assert second.visual == first.visual

    events = await chat_repository.list_events_after(0)
    assert len(events) == 1 and events[0].reply == first

    async with database.db_session() as session:
        # 消息不能重复插入。
        message_count = await session.scalar(
            select(func.count()).select_from(MessageRow)
        )
        assert message_count == 2

        # 轮状态、指针和版本不重复推进。
        turn = await session.get(TurnRow, saved.turn_id)
        assert turn.turn_state == "complete"
        assert turn.accepted_message_id == first.message_id
        assert turn.row_version == first_row_version
        assert turn.turn_token_count == 20
        assert turn.cumulative_token_count == 20

        # token 不能再次累计成 40。
        chat_session = await session.get(SessionRow, turn.session_id)
        assert chat_session.session_progress_tokens == 20

        attempt = await session.get(
            GenerationAttemptRow, ticket.attempt_id
        )
        assert attempt.attempt_state == "accepted"

        job = await session.get(GenerationJobRow, saved.job_id)
        assert job.state == "done"


@pytest.mark.asyncio
async def test_new_user_message_rejects_old_candidate(
    prepared_database,
):
    # 1. 准备：旧回复已有候选，但尚未正式采用。
    database, chat_repository, repository = prepared_database
    saved, ticket, candidate = await prepare_candidate(
        chat_repository, repository
    )

    # 2. 新用户消息先执行 T1 并提交。
    second_command = replace(
        make_message(),
        request_id="request_002",
        content="我还想聊一会儿",
    )
    second = await chat_repository.accept_message(second_command)

    assert second.turn_id == saved.turn_id
    assert second.input_version == 2

    # 3. 尝试采用旧输入版本的候选。
    result = await repository.accept_reply(
        ticket,
        turn_token_count=20,
        tokenizer_version="test-tokenizer",
    )

    # 4. 旧候选必须被拒绝。
    assert result is None
    assert await chat_repository.list_events_after(0) == ()

    async with database.db_session() as session:
        turn = await session.get(TurnRow, saved.turn_id)
        assert turn.turn_state == "current"
        assert turn.input_version == 2
        assert turn.accepted_message_id is None
        assert turn.turn_token_count == 0

        # 数据库只有两条用户输入，没有旧正式回复。
        messages = (
            await session.scalars(
                select(MessageRow)
                .where(MessageRow.turn_id == saved.turn_id)
                .order_by(MessageRow.message_seq)
            )
        ).all()

        assert [message.role for message in messages] == [
            "user",
            "user",
        ]
        assert [message.content for message in messages] == [
            "今天有点累",
            "我还想聊一会儿",
        ]

        # 旧任务失效。
        old_attempt = await session.get(
            GenerationAttemptRow, ticket.attempt_id
        )
        assert old_attempt.attempt_state == "superseded"

        old_job = await session.get(GenerationJobRow, saved.job_id)
        assert old_job.state == "obsolete"

        # 旧采用操作不能破坏新任务。
        new_job = await session.get(GenerationJobRow, second.job_id)
        assert new_job.state == "need_prepare"
        assert new_job.expected_input_version == 2

        chat_session = await session.get(SessionRow, turn.session_id)
        assert chat_session.session_progress_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"attempt_id": "missing_attempt"},
        {"input_version": 999},
        {"active_token": 999},
        {"prompt_hash": "wrong-prompt-hash"},
        {"candidate_hash": "wrong-candidate-hash"},
    ],
)
async def test_wrong_ticket_does_not_adopt_reply(
    prepared_database,
    changes,
):
    # 1. 准备合法候选，再只修改票据中的一个字段。
    database, chat_repository, repository = prepared_database
    saved, ticket, candidate = await prepare_candidate(
        chat_repository, repository
    )
    wrong_ticket = replace(ticket, **changes)

    # 记录拒绝前的轮版本，确认拒绝不会修改原轮次。
    async with database.db_session() as session:
        turn = await session.get(TurnRow, saved.turn_id)
        original_row_version = turn.row_version

    # 2. 当前实现中：
    # 身份/版本不匹配返回 None；材料 hash 不匹配抛业务异常。
    if "prompt_hash" in changes or "candidate_hash" in changes:
        with pytest.raises(InvalidStateError):
            await repository.accept_reply(
                wrong_ticket,
                turn_token_count=20,
                tokenizer_version="test-tokenizer",
            )
    else:
        result = await repository.accept_reply(
            wrong_ticket,
            turn_token_count=20,
            tokenizer_version="test-tokenizer",
        )
        assert result is None

    # 3. 检查：没有保存回复，也没有破坏原候选和任务。
    async with database.db_session() as session:
        message_count = await session.scalar(
            select(func.count()).select_from(MessageRow)
        )
        assert message_count == 1

        turn = await session.get(TurnRow, saved.turn_id)
        assert turn.turn_state == "current"
        assert turn.accepted_message_id is None
        assert turn.row_version == original_row_version
        assert turn.turn_token_count == 0

        attempt = await session.get(
            GenerationAttemptRow, ticket.attempt_id
        )
        assert attempt.attempt_state == "generating"
        assert attempt.candidate_json is not None

        job = await session.get(GenerationJobRow, saved.job_id)
        assert job.state == "need_accept"

        chat_session = await session.get(SessionRow, turn.session_id)
        assert chat_session.session_progress_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["job_update", "event_insert"])
async def test_t3_failure_rolls_back_all_changes(prepared_database, failure_point):
    """T3故障回滚"""
    # 1. 准备持久候选。
    database, chat_repository, repository = prepared_database
    saved, ticket, candidate = await prepare_candidate(
        chat_repository, repository
    )

    async with database.db_session() as session:
        turn = await session.get(TurnRow, saved.turn_id)
        original_row_version = turn.row_version

    # 2. 在临时测试库中制造 SQL 故障。
    # 当 T3 将 job 更新为 done 时失败。
    # 此时正式消息 INSERT 已经执行过，应该随整个事务回滚。
    async with database.transaction() as session:
        trigger_target = ("BEFORE UPDATE OF state ON generation_jobs WHEN NEW.state = 'done'"
                          if failure_point == "job_update" else "BEFORE INSERT ON chat_events")
        await session.execute(
            text(f"""
                CREATE TRIGGER fail_t3_done
                {trigger_target}
                BEGIN
                    SELECT RAISE(ABORT, 'simulated T3 failure');
                END;
            """)
        )

    # 3. 执行 T3，确认确实遇到存储异常。
    with pytest.raises(IntegrityError, match="simulated T3 failure"):
        await repository.accept_reply(
            ticket,
            turn_token_count=20,
            tokenizer_version="test-tokenizer",
        )

    # 4. 新建查询 Session，检查所有本次变化都被撤销。
    assert await chat_repository.list_events_after(0) == ()
    async with database.db_session() as session:
        # 正式回复 INSERT 被撤销，只保留原用户消息。
        messages = (
            await session.scalars(
                select(MessageRow)
                .where(MessageRow.turn_id == saved.turn_id)
            )
        ).all()

        assert len(messages) == 1
        assert messages[0].message_id == saved.message_id
        assert messages[0].role == "user"
        assert messages[0].content == "今天有点累"

        # 轮状态、指针、版本、计数恢复到 T3 前。
        turn = await session.get(TurnRow, saved.turn_id)
        assert turn.turn_state == "current"
        assert turn.accepted_message_id is None
        assert turn.row_version == original_row_version
        assert turn.turn_token_count == 0
        assert turn.cumulative_token_count == 0
        assert turn.tokenizer_version is None

        # 候选属于更早已提交的阶段，必须继续保留。
        attempt = await session.get(
            GenerationAttemptRow, ticket.attempt_id
        )
        assert attempt.attempt_state == "generating"
        assert attempt.candidate_json is not None
        assert attempt.completed_at is None

        job = await session.get(GenerationJobRow, saved.job_id)
        assert job.state == "need_accept"
        assert job.finished_at is None

        # session token 增量也必须撤销。
        chat_session = await session.get(SessionRow, turn.session_id)
        assert chat_session.session_progress_tokens == 0

    # 5. 排除故障后，原候选应该仍可重试采用。
    async with database.transaction() as session:
        await session.execute(text("DROP TRIGGER fail_t3_done"))

    result = await repository.accept_reply(
        ticket,
        turn_token_count=20,
        tokenizer_version="test-tokenizer",
    )
    assert result is not None
    events = await chat_repository.list_events_after(0)
    assert len(events) == 1 and events[0].reply == result

    async with database.db_session() as session:
        message_count = await session.scalar(
            select(func.count()).select_from(MessageRow)
        )
        assert message_count == 2

        turn = await session.get(TurnRow, saved.turn_id)
        assert turn.turn_state == "complete"
        assert turn.accepted_message_id == result.message_id

        job = await session.get(GenerationJobRow, saved.job_id)
        assert job.state == "done"

        chat_session = await session.get(SessionRow, turn.session_id)
        assert chat_session.session_progress_tokens == 20


@pytest.mark.asyncio
async def test_concurrent_adoption_emits_one_event(prepared_database):
    import asyncio
    database, chat, repository = prepared_database
    saved, ticket, _candidate = await prepare_candidate(chat, repository)
    first, second = await asyncio.gather(*[
        repository.accept_reply(ticket, turn_token_count=20, tokenizer_version="test-tokenizer")
        for _ in range(2)
    ])
    assert first is not None and second is not None
    assert first == second
    events = await chat.list_events_after(0)
    assert len(events) == 1
    assert events[0].reply == first
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(ChatEventRow)) == 1
        turn = await session.get(TurnRow, saved.turn_id)
        assert (await session.get(SessionRow, turn.session_id)).session_progress_tokens == 20
