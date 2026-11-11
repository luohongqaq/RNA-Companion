"""daily任务登记：幂等、来源日期和封存事务的一致性。"""
import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from module.shared.common.errors.base import IdempotencyConflictError, InvalidStateError, NotFoundError
from module.memory_management.shared.jobs.core.domain.job import MemoryJobStatus, MemoryJobType
from module.memory_management.daily.core.domain.job import DailyJobInput
from module.shared.memory.types.references import MemoryKind
from module.memory_management.daily.core.processing.service import DailyProductionService
from module.memory_management.daily.core.processing.config import DailyModelConfig
from module.memory_management.daily.core.domain.plan import DailyPlan
from module.memory_management.daily.core.domain.generation import DailyCandidate
from module.memory_management.daily.core.domain.document import DailyDocument, DailyMoment, MomentKind
from module.memory_management.shared.contracts.errors import MemorySourceChangedError
from module.memory_management.daily.core.scheduling.workflow import DailyWorkflow
from module.shared.jobs.config.retry import RetryPolicy
from module.shared.context.types.turns import TurnRange
from module.shared.memory.types.references import MemoryRevisionRef
from module.memory_management.shared.core.domain.document import DailyMemory
from module.shared.context.types.references import InheritedContextRef, SnapshotRef
from module.shared.context.types.summary import StructuredSummary
from module.context_compaction.repository import mappers as context_mappers
from module.memory_management.shared.repository import document_mappers
from module.memory_management.shared.jobs.repository import mappers as job_mappers
from storage.sqlite.value_codecs import SQLiteValueCodec
from storage.sqlite.db_session import SQLiteDatabase
from module.memory_management.daily.repository.sqlite import DailyProductionRepository
from storage.sqlite.models import ContextSnapshotRow, CharacterRow, ConversationRow, DailyMemoryRow, MemoryJobRow, MessageRow, SessionRow, TurnRow
from module.memory_management.daily.repository.llm import DailyModelRepository


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(tmp_path / "daily.sqlite3")
    await database.initialize()
    now = datetime(2026, 10, 29, 15, 59, tzinfo=timezone.utc)
    try:
        async with database.transaction() as session:
            session.add(CharacterRow(character_id="c", name="test", character_setting="test"))
            await session.flush()
            session.add(ConversationRow(conversation_id="conv", character_id="c", state="active",
                created_at=now, updated_at=now))
            await session.flush()
            for sid in ("s1", "s2"):
                session.add(SessionRow(session_id=sid, conversation_id="conv", session_seq=1 if sid == "s1" else 2, session_state="sealed",
                    session_start_time=now, session_end_time=now))
                await session.flush()
                session.add(TurnRow(turn_id=sid, session_id=sid, turn_seq=1, turn_state="complete"))
                await session.flush()
                session.add_all([
                    MessageRow(message_id=sid+"u", turn_id=sid, message_seq=1, role="user", content="你好", created_at=now),
                    MessageRow(message_id=sid+"a", turn_id=sid, message_seq=2, role="character", content="晚安",
                        created_at=datetime(2026, 10, 29, 16, 1, tzinfo=timezone.utc)),
                ])
                await session.flush()
                (await session.get(TurnRow, sid)).accepted_message_id = sid+"a"
                # 新一天开始的新轮，与上一轮跨午夜的回复属于不同daily。
                session.add(TurnRow(turn_id=sid+"next", session_id=sid, turn_seq=2, turn_state="complete"))
                await session.flush()
                session.add_all([
                    MessageRow(message_id=sid+"nextu", turn_id=sid+"next", message_seq=1, role="user",
                        content="早安", created_at=datetime(2026, 10, 29, 16, 5, tzinfo=timezone.utc)),
                    MessageRow(message_id=sid+"nexta", turn_id=sid+"next", message_seq=2, role="character",
                        content="早安", created_at=datetime(2026, 10, 29, 16, 6, tzinfo=timezone.utc)),
                ])
                await session.flush()
                (await session.get(TurnRow, sid+"next")).accepted_message_id = sid+"nexta"
        yield database, DailyProductionRepository(database)
    finally:
        await database.close()


def command(sid="s1", day=date(2026, 10, 29)):
    return DailyJobInput.create_command("c", sid, day, ())


@pytest.mark.asyncio
async def test_daily_registration_and_idempotent_replay_preserve_stage(store):
    database, repo = store
    request = command()
    job_id = await repo.create_job(request)
    async with database.transaction() as session:
        row = await session.get(MemoryJobRow, job_id)
        job = job_mappers.memory_job_to_domain(row)
        assert job.status == MemoryJobStatus.NEED_GENERATE
        assert job.input == request.input and job.attempt_version == job.attempt_count == 0
        assert job.candidate is None and job.source_manifest is None
        row.status = MemoryJobStatus.DONE.value
    assert await repo.create_job(request) == job_id
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 1
        assert (await session.get(MemoryJobRow, job_id)).status == "done"


@pytest.mark.asyncio
async def test_concurrent_duplicate_registration_has_one_row(store):
    _, repo = store
    ids = await asyncio.gather(repo.create_job(command()), repo.create_job(command()))
    assert ids[0] == ids[1]


@pytest.mark.asyncio
async def test_same_day_sessions_share_target_and_new_day_turn_gets_new_date(store):
    database, repo = store
    requests = (command(), command("s2"), command(day=date(2026, 10, 30)))
    ids = [await repo.create_job(request) for request in requests]
    assert len(set(ids)) == 3
    async with database.db_session() as session:
        rows = [await session.get(MemoryJobRow, job_id) for job_id in ids]
        assert rows[0].target_key == rows[1].target_key != rows[2].target_key
        assert rows[0].dedupe_key != rows[1].dedupe_key


@pytest.mark.asyncio
async def test_dedupe_input_conflict_does_not_overwrite(store):
    _, repo = store
    request = command()
    await repo.create_job(request)
    with pytest.raises(IdempotencyConflictError):
        await repo.create_job(replace(request, priority=20))


@pytest.mark.asyncio
async def test_source_date_session_and_keys_are_checked(store):
    database, repo = store
    request = command()
    with pytest.raises(InvalidStateError):
        await repo.create_job(replace(request, target_key="wrong"))
    with pytest.raises(NotFoundError):
        await repo.create_job(command(sid="missing"))
    with pytest.raises(InvalidStateError):
        await repo.create_job(command(day=date(2026, 11, 1)))
    with pytest.raises(InvalidStateError):
        await repo.create_job(replace(request, input=replace(request.input, source_message_ids=("s1a",))))
    async with database.transaction() as session:
        (await session.get(SessionRow, "s1")).session_state = "open"
    with pytest.raises(InvalidStateError, match="封存"):
        await repo.create_job(request)


@pytest.mark.asyncio
async def test_transactional_registration_rolls_back_with_sealing(store):
    database, repo = store
    async with database.transaction() as session:
        (await session.get(SessionRow, "s1")).session_state = "open"
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.transaction() as session:
            (await session.get(SessionRow, "s1")).session_state = "sealed"
            await repo.create_job_in_transaction(session, command())
            raise RuntimeError("rollback")
    async with database.db_session() as session:
        assert (await session.get(SessionRow, "s1")).session_state == "open"
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 0


@pytest.mark.asyncio
async def test_scan_uses_registration_marker_not_any_existing_daily_job(store):
    database, repo = store
    assert await repo.get_unregister_sealed_sessions(limit=1) == ["s1"]
    # 跨天session只登记了第一天，不能因此跳过第二天。
    await repo.create_job(command())
    assert await repo.get_unregister_sealed_sessions() == ["s1", "s2"]
    async with database.transaction() as session:
        (await session.get(SessionRow, "s1")).daily_jobs_registered_at = datetime.now(timezone.utc)
    assert await repo.get_unregister_sealed_sessions() == ["s2"]
    async with database.transaction() as session:
        (await session.get(SessionRow, "s2")).session_state = "waiting_handoff"
    assert await repo.get_unregister_sealed_sessions() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 257, True])
async def test_sealed_scan_rejects_invalid_limit(store, limit):
    _, repo = store
    with pytest.raises(ValueError):
        await repo.get_unregister_sealed_sessions(limit)


@pytest.mark.asyncio
async def test_read_sealed_source_keeps_cross_midnight_raw_and_excludes_inherited_material(store):
    database, repo = store
    async with database.transaction() as session:
        current = await session.get(SessionRow, "s2")
        current.predecessor_session_id = "s1"
        # 故意使用读取器无需解析的继承材料，证明不会递归展开。
        current.inherited_context_json = {"not_part_of_own_raw": True}
        session.add(MessageRow(message_id="unused", turn_id="s2", message_seq=3,
            role="character", content="未采用的角色回复", created_at=current.session_end_time))
    source = await repo.read_sealed_session_source("s2")
    assert source.session_id == "s2" and source.character_id == "c"
    assert len(source.turns) == 2 and source.turns[0].session_id == "s2"
    turn = source.turns[0]
    assert tuple(message.message_id for message in turn.user_messages) == ("s2u",)
    assert turn.accepted_reply.message_id == "s2a"
    assert turn.user_messages[0].created_at == datetime(2026, 10, 29, 15, 59, tzinfo=timezone.utc)
    assert turn.accepted_reply.created_at == datetime(2026, 10, 29, 16, 1, tzinfo=timezone.utc)
    assert source.group_daily_message_ids(zone=ZoneInfo("Asia/Shanghai"))[date(2026, 10, 29)] == (
        "s2u", "s2a",
    )


@pytest.mark.asyncio
async def test_sealed_source_rejects_open_or_incomplete_and_missing_sessions(store):
    database, repo = store
    with pytest.raises(NotFoundError):
        await repo.read_sealed_session_source("missing")
    async with database.transaction() as session:
        (await session.get(SessionRow, "s1")).session_state = "open"
    with pytest.raises(InvalidStateError, match="封存"):
        await repo.read_sealed_session_source("s1")
    async with database.transaction() as session:
        (await session.get(SessionRow, "s1")).session_state = "sealed"
        (await session.get(TurnRow, "s1")).turn_state = "current"
    with pytest.raises(InvalidStateError):
        await repo.read_sealed_session_source("s1")


@pytest.mark.asyncio
async def test_sealed_source_orders_turns_and_all_user_messages(store):
    database, repo = store
    now = datetime.now(timezone.utc)
    async with database.transaction() as session:
        session.add(TurnRow(turn_id="second", session_id="s1", turn_seq=3, turn_state="complete"))
        await session.flush()
        session.add_all([
            MessageRow(message_id="second-u2", turn_id="second", message_seq=2, role="user", content="后一句", created_at=now),
            MessageRow(message_id="second-u1", turn_id="second", message_seq=1, role="user", content="前一句", created_at=now),
            MessageRow(message_id="second-a", turn_id="second", message_seq=3, role="character", content="正式回复", created_at=now),
        ])
        await session.flush()
        (await session.get(TurnRow, "second")).accepted_message_id = "second-a"
    source = await repo.read_sealed_session_source("s1")
    assert tuple(turn.turn_seq for turn in source.turns) == (1, 2, 3)
    assert tuple(message.message_id for message in source.turns[2].user_messages) == ("second-u1", "second-u2")


@pytest.mark.asyncio
async def test_register_daily_jobs_groups_dates_marks_sessions_and_is_repeatable(store, monkeypatch):
    from module.shared.common.time import BackendTime

    local = BackendTime.local
    monkeypatch.setattr(BackendTime, "local", staticmethod(
        lambda value, *, zone=None: local(value, zone=zone or ZoneInfo("Asia/Shanghai"))
    ))
    database, repo = store
    service = DailyProductionService(repo)
    ids = await service.register_daily_jobs(priority=20)
    assert len(ids) == 4
    assert await service.register_daily_jobs() == ()
    async with database.db_session() as session:
        jobs = [job_mappers.memory_job_to_domain(await session.get(MemoryJobRow, job_id)) for job_id in ids]
        assert [job.input.memory_date for job in jobs] == [date(2026, 10, 29), date(2026, 10, 30)] * 2
        assert [job.input.source_message_ids for job in jobs] == [
            ("s1u", "s1a"), ("s1nextu", "s1nexta"),
            ("s2u", "s2a"), ("s2nextu", "s2nexta"),
        ]
        assert all(job.priority == 20 and job.input.base_revision is None for job in jobs)
        assert jobs[0].target_key == jobs[2].target_key
        assert jobs[0].dedupe_key != jobs[2].dedupe_key
        for sid in ("s1", "s2"):
            assert (await session.get(SessionRow, sid)).daily_jobs_registered_at is not None
    monkeypatch.setattr(BackendTime, "local", staticmethod(
        lambda value, *, zone=None: local(value, zone=zone or timezone.utc)
    ))
    resumed = await repo.read_daily_source(jobs[1])
    assert resumed.plan.memory_date == date(2026, 10, 30)
    assert resumed.source_manifest.source_message_ids == ("s1nextu", "s1nexta")


@pytest.mark.asyncio
async def test_register_daily_jobs_resumes_partial_registration(store):
    database, repo = store
    first = DailyJobInput.create_command("c", "s1", date(2026, 10, 29), ("s1u", "s1a"))
    first_id = await repo.create_job(first)
    ids = await DailyProductionService(repo).register_daily_jobs(limit=1)
    assert len(ids) == 2 and ids[0] == first_id
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 2
        assert (await session.get(SessionRow, "s1")).daily_jobs_registered_at is not None
        assert (await session.get(SessionRow, "s2")).daily_jobs_registered_at is None


@pytest.mark.asyncio
async def test_register_daily_jobs_rolls_back_one_session_and_continues_others(store, monkeypatch):
    database, repo = store
    original = repo.create_job_in_transaction

    async def fail_second_day(session, command):
        if command.input.source_session_ids == ("s1",) and command.input.memory_date == date(2026, 10, 30):
            raise TimeoutError("temporary failure")
        return await original(session, command)

    monkeypatch.setattr(repo, "create_job_in_transaction", fail_second_day)
    ids = await DailyProductionService(repo).register_daily_jobs()
    assert len(ids) == 2
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 2
        assert (await session.get(SessionRow, "s1")).daily_jobs_registered_at is None
        assert (await session.get(SessionRow, "s2")).daily_jobs_registered_at is not None
    monkeypatch.setattr(repo, "create_job_in_transaction", original)
    assert len(await DailyProductionService(repo).register_daily_jobs()) == 2


@pytest.mark.asyncio
async def test_atomic_registration_rejects_incomplete_date_batch(store):
    database, repo = store
    first_day = DailyJobInput.create_command("c", "s1", date(2026, 10, 29), ("s1u",))
    with pytest.raises(InvalidStateError, match="覆盖"):
        await repo.register_daily_jobs_for_session("s1", (first_day,))
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 0
        assert (await session.get(SessionRow, "s1")).daily_jobs_registered_at is None


@pytest.mark.asyncio
async def test_registration_cancel_propagates(store, monkeypatch):
    _, repo = store

    async def cancelled(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(repo, "read_sealed_session_source", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await DailyProductionService(repo).register_daily_jobs()


@pytest.mark.asyncio
async def test_empty_sealed_session_is_marked_without_creating_empty_daily(store):
    database, repo = store
    service = DailyProductionService(repo)
    await service.register_daily_jobs()
    async with database.transaction() as session:
        session.add(SessionRow(session_id="empty", conversation_id="conv", session_seq=3, session_state="sealed",
            session_start_time=datetime.now(timezone.utc), session_end_time=datetime.now(timezone.utc)))
    assert await service.register_daily_jobs() == ()
    assert await repo.get_unregister_sealed_sessions() == []
    async with database.db_session() as session:
        assert (await session.get(SessionRow, "empty")).daily_jobs_registered_at is not None
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 4


@pytest.mark.asyncio
async def test_concurrent_registration_does_not_duplicate_jobs(store):
    database, repo = store
    await asyncio.gather(DailyProductionService(repo).register_daily_jobs(),
                         DailyProductionService(repo).register_daily_jobs())
    assert await repo.get_unregister_sealed_sessions() == []
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 4


@pytest.mark.asyncio
async def test_first_user_message_owns_whole_turn_across_midnight(store):
    _, repo = store
    source = await repo.read_sealed_session_source("s1")
    turn = source.turns[0]
    followup = replace(turn.user_messages[0], message_id="followup", message_seq=2,
        created_at=turn.accepted_reply.created_at)
    whole_turn = replace(turn, user_messages=(*turn.user_messages, followup))
    source = replace(source, turns=(whole_turn,))
    assert source.group_daily_message_ids() == {date(2026, 10, 29): ("s1u", "followup", "s1a")}
    assert turn.accepted_reply.created_at == datetime(2026, 10, 29, 16, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_storage_rejects_splitting_a_turn_by_message_date(store):
    database, repo = store
    split = (
        DailyJobInput.create_command("c", "s1", date(2026, 10, 29), ("s1u",)),
        DailyJobInput.create_command("c", "s1", date(2026, 10, 30), ("s1a", "s1nextu", "s1nexta")),
    )
    with pytest.raises(InvalidStateError, match="完整轮"):
        await repo.register_daily_jobs_for_session("s1", split)
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 0
        assert (await session.get(SessionRow, "s1")).daily_jobs_registered_at is None


async def prepare_daily_attempt(repo, *, session_id="s1", second_day=False):
    day = date(2026, 10, 30) if second_day else date(2026, 10, 29)
    seq = 2 if second_day else 1
    job_id = await repo.create_job(command(sid=session_id, day=day))
    plan = DailyPlan(session_id, "c", day, TurnRange(session_id, seq, seq),
        TurnRange(session_id, 1, 1) if second_day else None)
    return job_id, plan


@pytest.mark.asyncio
@pytest.mark.parametrize("second_day", [False, True])
async def test_generation_prepares_source_once_and_saves_checkpoint(store, second_day, monkeypatch):
    _, repo = store
    job_id, expected = await prepare_daily_attempt(repo, second_day=second_day)
    original = repo._read_sealed_session_source
    reads = []

    async def read_source(session, session_id):
        reads.append(session_id)
        return await original(session, session_id)

    monkeypatch.setattr(repo, "_read_sealed_session_source", read_source)

    async def generate(source):
        assert source.plan == expected
        during = await repo.get_job(job_id)
        assert during.status == MemoryJobStatus.GENERATING
        assert during.plan is during.source_manifest is during.candidate is None
        assert source.source_manifest.source_message_ids == (("s1nextu", "s1nexta") if second_day else ("s1u", "s1a"))
        return saved_daily_candidate(during)

    service = DailyProductionService(repo, daily_generator=generate)
    await service.process_job(job_id)
    saved = await repo.get_job(job_id)
    assert reads == ["s1"]
    assert saved.status == MemoryJobStatus.NEED_REVIEW
    assert saved.plan == expected and saved.source_manifest is not None and saved.candidate is not None
    # 审核从已提交的候选继续，不重新生成。
    source = await repo.read_daily_source(saved)
    assert source.plan == expected and source.source_manifest == saved.source_manifest
    assert tuple(turn.turn_seq for turn in source.context_only_turns) == ((1,) if second_day else ())


def daily_character_resources():
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from module.character.core.domain.models import CharacterCard

    directory = Path(__file__).resolve().parents[4] / "resources/characters/character_deepseek"
    card = CharacterCard("c", "deepseek", (directory / "角色卡.md").read_text(encoding="utf-8-sig"), directory)
    return SimpleNamespace(load=AsyncMock(return_value=SimpleNamespace(character_card=card)))


def daily_model_client(result, *, finish_reason="stop"):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
        message=SimpleNamespace(content=content))]))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def daily_model_result(turn_id="s1"):
    return {"session_id": "s1", "title": "值得记住的事",
            "memorable_moments": [{"kind": "milestone", "text": "用户和我说起一个重要进展。",
                                   "source_turn_ids": [turn_id]}],
            "profile_facts": [], "intentions": [], "commitments": [], "reflections": []}


@pytest.mark.asyncio
async def test_default_daily_model_builds_prompt_and_commits_candidate(store):
    from types import SimpleNamespace
    from module.memory_management.daily.core.processing.prompts.generation import GENERATION_PROMPT

    _, repo = store
    job_id, expected = await prepare_daily_attempt(repo, second_day=True)
    client = daily_model_client(daily_model_result("s1next"))
    service = DailyProductionService(repo, model=DailyModelRepository(client), prompt_resources=daily_character_resources())
    await service.process_job(job_id)
    saved = await repo.get_job(job_id)
    assert saved.status == MemoryJobStatus.NEED_REVIEW and saved.plan == expected
    assert saved.candidate.content_hash == saved.candidate.document.content_hash()
    assert saved.candidate.document.memory_date == expected.memory_date
    assert saved.candidate.document.source_session_ids == ("s1",)
    assert saved.candidate.document.memorable_moments[0].source_turn_ids == ("s1next",)
    request = client.chat.completions.create.await_args.kwargs
    assert request["model"] == "glm-5.3" and request["reasoning_effort"] == "low"
    assert request["extra_body"]["thinking"]["type"] == "enabled"
    assert request["messages"][0] == {"role": "system", "content": GENERATION_PROMPT}
    payload = json.loads(request["messages"][1]["content"])
    assert payload["SESSION_META"]["memory_date"] == "2026-10-30"
    assert [turn["turn_id"] for turn in payload["SESSION_TURNS"]] == ["s1next"]
    assert [turn["turn_id"] for turn in payload["CONTEXT_ONLY_TURNS"]] == ["s1"]
    assert payload["ALLOWED_SOURCE_TURN_IDS"] == ["s1next"]
    assert payload["BASE_DAILY"] is None and "DAILY_BUDGET" not in payload
    assert payload["CHARACTER_CONTEXT"]["character_name"] == "deepseek"


@pytest.mark.asyncio
async def test_daily_model_carries_existing_daily_sources_without_changing_ownership(store):
    from types import SimpleNamespace

    database, repo = store
    job_id, plan = await prepare_daily_attempt(repo)
    old = DailyDocument(plan.memory_date, ("previous",), "此前经历",
                        (DailyMoment(MomentKind.MILESTONE, "此前用户和我谈了项目。", ("old-turn",)),))
    now = datetime.now(timezone.utc)
    memory = DailyMemory(document_id="daily", kind=MemoryKind.DAILY, current_revision=1, content_hash="old-hash",
        vector_document_key="daily:1", published_at=now, updated_at=now, memory_date=plan.memory_date,
        source_session_ids=("previous",), document=old)
    async with database.transaction() as session:
        session.add(document_mappers.daily_memory_to_row(memory, character_id="c"))
    result = daily_model_result()
    result["memorable_moments"].append({"kind": "milestone", "text": "此前用户和我谈了项目。", "source_turn_ids": ["old-turn"]})
    client = daily_model_client(result)
    service = DailyProductionService(repo, model=DailyModelRepository(client), prompt_resources=daily_character_resources(),
        )
    await service.process_job(job_id)
    saved = await repo.get_job(job_id)
    assert saved.candidate.document.source_session_ids == ("previous", "s1")
    assert saved.candidate.document.memorable_moments[1].source_turn_ids == ("old-turn",)
    assert saved.source_manifest.source_message_ids == ("s1u", "s1a")
    payload = json.loads(client.chat.completions.create.await_args.kwargs["messages"][1]["content"])
    assert payload["BASE_DAILY"]["memorable_moments"][0]["text"] == old.memorable_moments[0].text
    assert set(payload["ALLOWED_SOURCE_TURN_IDS"]) == {"old-turn", "s1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["json", "truncated", "session", "context_ref", "missing_flags"])
async def test_invalid_daily_model_results_are_not_saved_and_retry_generation(store, failure):
    from datetime import timedelta
    from types import SimpleNamespace

    _, repo = store
    job_id, _ = await prepare_daily_attempt(repo, second_day=True)
    result = daily_model_result("s1next")
    if failure == "json":
        result = "{broken"
    elif failure == "session":
        result["session_id"] = "other"
    elif failure == "context_ref":
        result["memorable_moments"][0]["source_turn_ids"] = ["s1"]
    elif failure == "missing_flags":
        del result["reflections"]
    client = daily_model_client(result, finish_reason="length" if failure == "truncated" else "stop")
    service = DailyProductionService(repo, model=DailyModelRepository(client), prompt_resources=daily_character_resources(),
        )
    workflow = DailyWorkflow(repo, service, retry_policy=RetryPolicy(max_retries=1, delays=(timedelta(0),)))
    assert await workflow.execute(job_id)
    saved = await repo.get_job(job_id)
    assert saved.status == MemoryJobStatus.NEED_GENERATE and saved.retry_count == 1
    assert saved.candidate is saved.plan is saved.source_manifest is None


@pytest.mark.asyncio
async def test_daily_model_missing_character_resources_does_not_start_attempt(store):
    _, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    before = await repo.get_job(job_id)
    service = DailyProductionService(repo, model=DailyModelRepository(daily_model_client(daily_model_result())))
    with pytest.raises(NotImplementedError, match="角色资源"):
        await service.process_job(job_id)
    assert await repo.get_job(job_id) == before


@pytest.mark.asyncio
async def test_daily_model_configuration_controls_request(store):
    from types import SimpleNamespace

    _, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    client = daily_model_client(daily_model_result())
    service = DailyProductionService(repo, model=DailyModelRepository(client), prompt_resources=daily_character_resources(),
        model_config=DailyModelConfig(model_name="configured-ds", disable_thinking=True))
    await service.process_job(job_id)
    request = client.chat.completions.create.await_args.kwargs
    assert request["model"] == "configured-ds" and "reasoning_effort" not in request
    assert request["extra_body"]["thinking"]["type"] == "disabled"


@pytest.mark.asyncio
async def test_empty_daily_is_valid_without_inventing_facts(store):
    _, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    result = {"session_id": "s1", "title": "没有新增的长期记忆", "memorable_moments": [],
              "profile_facts": [], "intentions": [], "commitments": [], "reflections": []}
    service = DailyProductionService(repo, model=DailyModelRepository(daily_model_client(result)),
        prompt_resources=daily_character_resources())
    await service.process_job(job_id)
    saved = await repo.get_job(job_id)
    assert saved.status == MemoryJobStatus.NEED_REVIEW and not saved.candidate.document.has_memories


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", ["你好", "晚安", "改写过的你好"])
async def test_profile_quotes_are_saved_for_model_review_without_program_text_matching(store, quote):
    from datetime import timedelta

    _, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    result = daily_model_result()
    result["profile_facts"] = [{"kind": "background", "text": "用户在这次交流中说了你好",
        "source_turn_ids": ["s1"], "evidence_quotes": [quote]}]
    service = DailyProductionService(repo, model=DailyModelRepository(daily_model_client(result)),
        prompt_resources=daily_character_resources())
    flow = DailyWorkflow(repo, service, retry_policy=RetryPolicy(max_retries=1, delays=(timedelta(0),)))
    assert await flow.execute(job_id)
    saved = await repo.get_job(job_id)
    assert saved.status == MemoryJobStatus.NEED_REVIEW
    assert saved.candidate.document.profile_facts[0].evidence_quotes == (quote,)


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", ["明天开始整理项目", "明天开始整理项目，做完后继续开发网站", "好，整理完再聊"])
async def test_open_loop_evidence_is_left_to_model_review(store, quote):
    from datetime import timedelta

    database, repo = store
    async with database.transaction() as session:
        (await session.get(MessageRow, "s1u")).content = "明天开始整理项目"
        (await session.get(MessageRow, "s1a")).content = "好，整理完再聊"
    job_id, _ = await prepare_daily_attempt(repo)
    result = daily_model_result()
    result["intentions"] = [{"owner": "user", "certainty": "plan", "status": "open",
        "text": "用户打算明天整理项目。", "target_date": "2026-10-30", "source_turn_ids": ["s1"], "evidence_quotes": [quote]}]
    service = DailyProductionService(repo, model=DailyModelRepository(daily_model_client(result)),
        prompt_resources=daily_character_resources())
    flow = DailyWorkflow(repo, service, retry_policy=RetryPolicy(max_retries=1, delays=(timedelta(0),)))
    assert await flow.execute(job_id)
    saved = await repo.get_job(job_id)
    assert saved.status == MemoryJobStatus.NEED_REVIEW
    assert saved.candidate.document.intentions[0].text == "用户打算明天整理项目。"
    assert saved.candidate.document.intentions[0].evidence_quotes == (quote,)




@pytest.mark.asyncio
async def test_daily_has_no_business_length_limit(store):
    _, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    result = daily_model_result()
    result["memorable_moments"][0]["text"] = "有依据的长期记忆内容" * 1000
    service = DailyProductionService(repo, model=DailyModelRepository(daily_model_client(result)),
        prompt_resources=daily_character_resources())
    await service.process_job(job_id)
    saved = await repo.get_job(job_id)
    assert saved.status == MemoryJobStatus.NEED_REVIEW
    assert saved.candidate.document.memorable_moments[0].text == result["memorable_moments"][0]["text"]


@pytest.mark.parametrize("field,value", [("owner", "unknown"), ("certainty", "certain"),
    ("status", "maybe_done"), ("target_date", "明天")])
def test_intentions_reject_invalid_subject_certainty_status_or_date(field, value):
    result = daily_model_result()
    item = {"owner": "user", "certainty": "plan", "status": "open", "text": "用户打算整理项目",
            "target_date": "2026-10-31", "source_turn_ids": ["s1"], "evidence_quotes": ["明天整理项目"]}
    item[field] = value
    result["intentions"] = [item]
    with pytest.raises(InvalidStateError):
        DailyDocument.from_model_data(result, session_id="s1", memory_date=date(2026, 10, 30),
                                      source_session_ids=("s1",), allowed_turn_ids={"s1"})


@pytest.mark.asyncio
@pytest.mark.parametrize("status,retryable", [(429, True), (401, False)])
async def test_daily_model_request_errors_use_existing_failure_flow(store, status, retryable):
    from datetime import timedelta
    from types import SimpleNamespace

    _, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    client = daily_model_client(daily_model_result())
    error = RuntimeError("private response")
    error.status_code = status
    client.chat.completions.create.side_effect = error
    service = DailyProductionService(repo, model=DailyModelRepository(client), prompt_resources=daily_character_resources(),
        )
    flow = DailyWorkflow(repo, service, retry_policy=RetryPolicy(max_retries=1, delays=(timedelta(0),)))
    assert await flow.execute(job_id)
    saved = await repo.get_job(job_id)
    assert saved.status == (MemoryJobStatus.NEED_GENERATE if retryable else MemoryJobStatus.FAILED)
    assert saved.candidate is None and "private response" not in saved.last_error


@pytest.mark.asyncio
async def test_unconfigured_generator_does_not_start_attempt(store):
    _, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    before = await repo.get_job(job_id)
    with pytest.raises(NotImplementedError, match="模型尚未接入"):
        await DailyProductionService(repo).process_job(job_id)
    assert await repo.get_job(job_id) == before


@pytest.mark.asyncio
async def test_begin_only_registers_attempt_and_does_not_read_sources(store, monkeypatch):
    _, repo = store
    job_id, _ = await prepare_daily_attempt(repo)

    async def unexpected_read(*args):
        raise AssertionError("开始代次不应读取来源")

    monkeypatch.setattr(repo, "_read_sealed_session_source", unexpected_read)
    started = await repo.begin_generate(job_id)
    assert started.status == MemoryJobStatus.GENERATING
    assert started.plan is started.source_manifest is started.candidate is None
    assert started.attempt_version == started.attempt_count == 1
    assert await repo.begin_generate(job_id) is None
    with pytest.raises(NotFoundError):
        await repo.begin_generate("missing")


@pytest.mark.asyncio
async def test_preparing_source_does_not_write_intermediate_checkpoint(store):
    _, repo = store
    job_id, plan = await prepare_daily_attempt(repo, second_day=True)
    started = await repo.begin_generate(job_id)
    source = await repo.read_daily_source(started)
    assert source.plan == plan and source.base_daily is None
    assert await repo.get_job(job_id) == started


@pytest.mark.asyncio
async def test_daily_planning_is_pure_and_rejects_non_contiguous_dates(store):
    _, repo = store
    source = await repo.read_sealed_session_source("s1")
    assert DailyPlan.plan_daily(source, memory_date=date(2026, 10, 30)).source_turns == TurnRange("s1", 2, 2)
    with pytest.raises(InvalidStateError, match="没有可提取"):
        DailyPlan.plan_daily(source, memory_date=date(2026, 10, 31))
    third = replace(source.turns[0], turn_id="third", turn_seq=3)
    with pytest.raises(InvalidStateError, match="不连续"):
        DailyPlan.plan_daily(replace(source, turns=(*source.turns, third)), memory_date=date(2026, 10, 29))


@pytest.mark.asyncio
async def test_interrupted_generation_prepares_current_materials_on_retry(store):
    database, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    first = await repo.begin_generate(job_id)
    old_source = await repo.read_daily_source(first)
    assert await repo.retry_job(job_id, first.attempt_version, expected_status=first.status,
        available_at=datetime.now(timezone.utc), error="interrupted")
    async with database.transaction() as session:
        (await session.get(MessageRow, "s1u")).content = "新的原文"
    restarted = await repo.begin_generate(job_id)
    source = await repo.read_daily_source(restarted)
    assert source.source_manifest != old_source.source_manifest
    assert restarted.plan is restarted.source_manifest is None
    assert not await repo.save_candidate(job_id, first.attempt_version, saved_daily_candidate(first), source=old_source)


@pytest.mark.asyncio
async def test_candidate_checkpoint_is_atomic_idempotent_and_not_replaced(store, monkeypatch):
    _, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    started = await repo.begin_generate(job_id)
    source = await repo.read_daily_source(started)
    candidate = saved_daily_candidate(started)
    original = job_mappers.memory_job_payload_to_json

    def fail_encode(_):
        raise RuntimeError("encoding failed")

    monkeypatch.setattr(job_mappers, "memory_job_payload_to_json", fail_encode)
    with pytest.raises(RuntimeError):
        await repo.save_candidate(job_id, started.attempt_version, candidate, source=source)
    assert await repo.get_job(job_id) == started
    monkeypatch.setattr(job_mappers, "memory_job_payload_to_json", original)
    assert await repo.save_candidate(job_id, started.attempt_version, candidate, source=source)
    saved = await repo.get_job(job_id)
    assert saved.plan == source.plan and saved.source_manifest == source.source_manifest
    assert saved.candidate == candidate and saved.status == MemoryJobStatus.NEED_REVIEW
    assert await repo.save_candidate(job_id, started.attempt_version, candidate, source=source)
    assert not await repo.save_candidate(job_id, started.attempt_version, replace(candidate, content_hash="other"), source=source)
    assert await repo.get_job(job_id) == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owned", "context", "revision"])
async def test_saved_candidate_requires_same_source_during_review(store, change):
    database, repo = store
    job_id, _ = await prepare_daily_attempt(repo, second_day=True)
    started = await repo.begin_generate(job_id)
    source = await repo.read_daily_source(started)
    assert await repo.save_candidate(job_id, started.attempt_version, saved_daily_candidate(started), source=source)
    saved = await repo.get_job(job_id)
    async with database.transaction() as session:
        if change == "revision":
            (await session.get(TurnRow, "s1")).content_revision += 1
        else:
            (await session.get(MessageRow, "s1nextu" if change == "owned" else "s1u")).content = "edited"
    with pytest.raises(MemorySourceChangedError, match="来源已变化"):
        await repo.read_daily_source(saved)
    assert await repo.get_job(job_id) == saved


@pytest.mark.asyncio
async def test_source_loads_current_base_daily_and_detects_changes_before_save(store):
    database, repo = store
    job_id, expected = await prepare_daily_attempt(repo)
    now = datetime.now(timezone.utc)
    document = DailyDocument(expected.memory_date, ("previous",), "已有daily", (DailyMoment(MomentKind.MILESTONE, "历史记忆", ("old-turn",)),))
    memory = DailyMemory(document_id="daily", kind=MemoryKind.DAILY, current_revision=2,
        content_hash="base-hash", vector_document_key="daily:2", published_at=now, updated_at=now,
        memory_date=expected.memory_date, source_session_ids=("previous",), document=document)
    async with database.transaction() as session:
        session.add(document_mappers.daily_memory_to_row(memory, character_id="c"))
    started = await repo.begin_generate(job_id)
    source = await repo.read_daily_source(started)
    assert source.base_daily == document
    assert source.plan.base_daily_ref == MemoryRevisionRef(MemoryKind.DAILY, "daily", 2, "base-hash")
    async with database.transaction() as session:
        row = await session.get(DailyMemoryRow, "daily")
        row.document_json = dict(row.document_json, memorable_moments=[{"kind": "milestone", "text": "新正文", "source_turn_ids": ["old-turn"]}])
    with pytest.raises(MemorySourceChangedError, match="基础版本已变化"):
        await repo.save_candidate(job_id, started.attempt_version, saved_daily_candidate(started), source=source)
    assert await repo.get_job(job_id) == started


@pytest.mark.asyncio
async def test_inherited_materials_are_context_only_and_saved_with_candidate(store):
    database, repo = store
    inherited = InheritedContextRef("s1", SnapshotRef("snapshot", "summary-hash", 1),
        TurnRange("s1", 2, 2), "fixed-source-hash")
    async with database.transaction() as session:
        session.add(ContextSnapshotRow(snapshot_id="snapshot", session_id="s1", covered_through_turn_seq=1,
            summary_json=context_mappers.structured_summary_to_json(StructuredSummary("前文摘要", keywords=("前文",))),
            rendered_summary_text="前文摘要", content_hash="summary-hash", snapshot_state="valid",
            created_at=datetime.now(timezone.utc), rendered_token_count=10))
        row = await session.get(SessionRow, "s2")
        row.predecessor_session_id = "s1"
        row.inherited_context_json = SQLiteValueCodec.inherited_context_to_json(inherited)
    job_id, _ = await prepare_daily_attempt(repo, session_id="s2")
    started = await repo.begin_generate(job_id)
    source = await repo.read_daily_source(started)
    assert source.inherited_context.ref == inherited
    assert source.inherited_context.snapshot.summary_text == "前文摘要"
    assert tuple(turn.turn_seq for turn in source.inherited_context.complete_turns) == (2,)
    assert source.source_manifest.source_message_ids == ("s2u", "s2a")
    document = DailyDocument(source.plan.memory_date, ("s2",), "当次", (DailyMoment(MomentKind.MILESTONE, "当次记忆", ("s2",)),))
    candidate = DailyCandidate(1, document, "hash", datetime.now(timezone.utc))
    assert await repo.save_candidate(job_id, started.attempt_version, candidate, source=source)
    async with database.transaction() as session:
        (await session.get(ContextSnapshotRow, "snapshot")).rendered_summary_text = "摘要改变"
    with pytest.raises(MemorySourceChangedError, match="来源已变化"):
        await repo.read_daily_source(await repo.get_job(job_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [MemoryJobStatus.NEED_REVIEW, MemoryJobStatus.REVIEWING,
    MemoryJobStatus.NEED_WRITE, MemoryJobStatus.WRITING, MemoryJobStatus.WAITING_INDEX,
    MemoryJobStatus.NEED_ACTIVATE, MemoryJobStatus.DONE,
    MemoryJobStatus.FAILED, MemoryJobStatus.OBSOLETE])
async def test_begin_does_not_rewind_other_stages(store, status):
    database, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    async with database.transaction() as session:
        (await session.get(MemoryJobRow, job_id)).status = status.value
    before = await repo.get_job(job_id)
    assert await repo.begin_generate(job_id) is None
    assert await repo.get_job(job_id) == before


@pytest.mark.asyncio
async def test_begin_respects_backoff_and_same_target_head(store):
    from datetime import timedelta

    database, repo = store
    first_id, _ = await prepare_daily_attempt(repo)
    second_id, _ = await prepare_daily_attempt(repo, session_id="s2")
    async with database.transaction() as session:
        (await session.get(MemoryJobRow, first_id)).available_at = datetime.now(timezone.utc)+timedelta(hours=1)
    assert await repo.begin_generate(first_id) is None
    assert await repo.begin_generate(second_id) is None
    async with database.transaction() as session:
        (await session.get(MemoryJobRow, first_id)).status = "done"
    assert await repo.begin_generate(second_id) is not None


def saved_daily_candidate(job):
    return DailyCandidate(1, DailyDocument(job.input.memory_date, ("s1",), "日常", (DailyMoment(MomentKind.MILESTONE, "记忆", ("s1",)),)),
        "candidate-hash", datetime.now(timezone.utc))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,target,invalidate", [
    (MemoryJobStatus.GENERATING, MemoryJobStatus.NEED_GENERATE, True),
    (MemoryJobStatus.REVIEWING, MemoryJobStatus.NEED_REVIEW, True),
    (MemoryJobStatus.WRITING, MemoryJobStatus.NEED_WRITE, True),
    (MemoryJobStatus.NEED_REVIEW, MemoryJobStatus.NEED_REVIEW, False),
    (MemoryJobStatus.NEED_ACTIVATE, MemoryJobStatus.NEED_ACTIVATE, False),
])
async def test_retry_preserves_material_and_releases_only_current_running_phase(store, status, target, invalidate):
    database, repo = store
    job_id, plan = await prepare_daily_attempt(repo)
    started = await repo.begin_generate(job_id)
    if status != MemoryJobStatus.GENERATING:
        source = await repo.read_daily_source(started)
        assert await repo.save_candidate(job_id, started.attempt_version, saved_daily_candidate(started), source=source)
        started = await repo.get_job(job_id)
    before = replace(started, status=status)
    async with database.transaction() as session:
        row = await session.get(MemoryJobRow, job_id)
        row.status, row.payload_json = status.value, job_mappers.memory_job_payload_to_json(before)
    assert await repo.retry_job(job_id, before.attempt_version, expected_status=status,
        available_at=datetime.now(timezone.utc), error="timeout", expected_retry_count=0)
    after = await repo.get_job(job_id)
    assert after.status == target and after.attempt_version == before.attempt_version+int(invalidate)
    assert after.attempt_count == before.attempt_count and after.retry_count == 1
    assert after.plan == before.plan and after.source_manifest == before.source_manifest and after.candidate == before.candidate
    assert not await repo.retry_job(job_id, before.attempt_version, expected_status=status,
        available_at=datetime.now(timezone.utc), error="late", expected_retry_count=0)
    assert await repo.get_job(job_id) == after


@pytest.mark.asyncio
async def test_source_change_replan_clears_derived_material_but_keeps_registration(store):
    database, repo = store
    job_id, plan = await prepare_daily_attempt(repo)
    started = await repo.begin_generate(job_id)
    before = replace(started, status=MemoryJobStatus.NEED_REVIEW, candidate=saved_daily_candidate(started),
        reviewed_version=1, reviewed_hash="candidate-hash", reextraction_count=1)
    async with database.transaction() as session:
        row = await session.get(MemoryJobRow, job_id)
        row.status, row.payload_json = before.status.value, job_mappers.memory_job_payload_to_json(before)
    assert await repo.retry_job(job_id, before.attempt_version, expected_status=before.status,
        available_at=datetime.now(timezone.utc), error="source changed", restart_generation=True,
        expected_retry_count=0)
    after = await repo.get_job(job_id)
    assert after.status == MemoryJobStatus.NEED_GENERATE and after.attempt_version == 2
    assert after.plan is after.source_manifest is after.candidate is after.review is None
    assert after.reviewed_hash is after.reviewed_version is None
    assert after.input == before.input and after.dedupe_key == before.dedupe_key
    assert after.reextraction_count == 1 and after.retry_count == 1


@pytest.mark.asyncio
async def test_model_failure_retries_from_generation_and_keeps_saved_candidate_on_late_error(store):
    from datetime import timedelta

    _, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    calls = []

    async def generate(source):
        calls.append(source)
        if len(calls) == 1:
            raise TimeoutError()
        return saved_daily_candidate(await repo.get_job(job_id))

    service = DailyProductionService(repo, daily_generator=generate)
    flow = DailyWorkflow(repo, service, retry_policy=RetryPolicy(max_retries=2, delays=(timedelta(0),)))
    assert await flow.execute(job_id)
    reset = await repo.get_job(job_id)
    assert reset.status == MemoryJobStatus.NEED_GENERATE
    assert reset.plan is reset.source_manifest is reset.candidate is None
    assert reset.retry_count == 1
    assert await flow.execute(job_id)
    saved = await repo.get_job(job_id)
    assert saved.status == MemoryJobStatus.NEED_REVIEW and saved.candidate is not None
    assert len(calls) == 2 and saved.attempt_count == 2
    assert not await flow._handle_failure(reset, TimeoutError())
    assert await repo.get_job(job_id) == saved


@pytest.mark.asyncio
async def test_interrupted_model_call_recovers_and_saved_candidate_survives_restart(store):
    _, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    calls = 0

    async def generate(source):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError()
        return saved_daily_candidate(await repo.get_job(job_id))

    service = DailyProductionService(repo, daily_generator=generate)
    with pytest.raises(asyncio.CancelledError):
        await service.process_job(job_id)
    interrupted = await repo.get_job(job_id)
    assert interrupted.status == MemoryJobStatus.GENERATING
    assert interrupted.plan is interrupted.source_manifest is interrupted.candidate is None
    assert await repo.recover_unfinished_attempts() == 1
    reset = await repo.get_job(job_id)
    assert reset.status == MemoryJobStatus.NEED_GENERATE and reset.attempt_count == 1
    assert reset.retry_count == 0 and reset.attempt_version > interrupted.attempt_version
    await service.process_job(job_id)
    saved = await repo.get_job(job_id)
    assert saved.status == MemoryJobStatus.NEED_REVIEW and calls == 2
    assert await repo.recover_unfinished_attempts() == 0
    assert await repo.get_job(job_id) == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("status,target", [
    (MemoryJobStatus.REVIEWING, MemoryJobStatus.NEED_REVIEW),
    (MemoryJobStatus.WRITING, MemoryJobStatus.NEED_WRITE),
])
async def test_startup_recovery_preserves_completed_model_checkpoint(store, status, target):
    database, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    started = await repo.begin_generate(job_id)
    source = await repo.read_daily_source(started)
    assert await repo.save_candidate(job_id, started.attempt_version, saved_daily_candidate(started), source=source)
    saved = await repo.get_job(job_id)
    async with database.transaction() as session:
        (await session.get(MemoryJobRow, job_id)).status = status.value
    assert await repo.recover_unfinished_attempts() == 1
    recovered = await repo.get_job(job_id)
    assert recovered.status == target and recovered.candidate == saved.candidate
    assert recovered.plan == saved.plan and recovered.source_manifest == saved.source_manifest
    assert recovered.attempt_count == saved.attempt_count and recovered.retry_count == saved.retry_count
    assert await repo.recover_unfinished_attempts() == 0


@pytest.mark.asyncio
async def test_fail_job_has_phase_version_guards_and_preserves_candidate(store):
    database, repo = store
    job_id, plan = await prepare_daily_attempt(repo)
    started = await repo.begin_generate(job_id)
    source = await repo.read_daily_source(started)
    assert await repo.save_candidate(job_id, started.attempt_version, saved_daily_candidate(started), source=source)
    before = await repo.get_job(job_id)
    assert not await repo.fail_job(job_id, 0, "late", expected_status=before.status)
    assert not await repo.fail_job(job_id, 1, "late", expected_status=MemoryJobStatus.GENERATING)
    assert not await repo.fail_job(job_id, 1, "late", expected_status=before.status, expected_retry_count=1)
    assert await repo.fail_job(job_id, 1, "permanent", expected_status=before.status, expected_retry_count=0)
    failed = await repo.get_job(job_id)
    assert failed.status == MemoryJobStatus.FAILED and failed.finished_at is not None
    assert failed.candidate == before.candidate and failed.plan == plan
    assert await repo.fail_job(job_id, 1, "permanent", expected_status=before.status)
    assert not await repo.fail_job(job_id, 1, "different", expected_status=before.status)
    assert await repo.get_job(job_id) == failed


@pytest.mark.asyncio
async def test_published_daily_is_preserved_when_export_retries_or_fails(store):
    database, repo = store
    job_id, plan = await prepare_daily_attempt(repo)
    started = await repo.begin_generate(job_id)
    now = datetime.now(timezone.utc)
    daily = DailyMemory(document_id="published", kind=MemoryKind.DAILY, current_revision=1,
        content_hash="published-hash", vector_document_key="published:1", published_at=now, updated_at=now,
        memory_date=plan.memory_date, source_session_ids=("s1",),
        document=DailyDocument(plan.memory_date, ("s1",), "已发布daily", (DailyMoment(MomentKind.MILESTONE, "历史记忆", ("s1",)),)))
    before = replace(started, status=MemoryJobStatus.NEED_ACTIVATE, applied_revision=1,
        result=MemoryRevisionRef(MemoryKind.DAILY, "published", 1, "published-hash"))
    async with database.transaction() as session:
        session.add(document_mappers.daily_memory_to_row(daily, character_id="c"))
        row = await session.get(MemoryJobRow, job_id)
        row.status, row.payload_json = before.status.value, job_mappers.memory_job_payload_to_json(before)
    assert not await repo.retry_job(job_id, 1, expected_status=before.status, available_at=now,
        error="source changed", restart_generation=True)
    assert await repo.retry_job(job_id, 1, expected_status=before.status, available_at=now, error="export timeout")
    retrying = await repo.get_job(job_id)
    assert retrying.status == MemoryJobStatus.NEED_ACTIVATE and retrying.result == before.result
    assert await repo.fail_job(job_id, 1, "export failed", expected_status=before.status, expected_retry_count=1)
    assert (await repo.get_job(job_id)).result == before.result
    async with database.db_session() as session:
        assert document_mappers.daily_memory_to_domain(await session.get(DailyMemoryRow, "published")) == daily


@pytest.mark.parametrize("certainty", ["wish", "plan"])
def test_daily_formal_markdown_omits_internal_evidence_and_keeps_reflections_separate(certainty):
    result = daily_model_result()
    result["profile_facts"] = [{"kind": "achievement", "text": "用户取得了一项长期成就",
        "source_turn_ids": ["s1"], "evidence_quotes": ["内部审核的证据原话"]}]
    result["intentions"] = [{"owner": "user", "certainty": certainty, "status": "open",
        "text": "用户想学习一种新技能", "target_date": None, "source_turn_ids": ["s1"], "evidence_quotes": ["内部意向原话"]}]
    result["commitments"] = [{"owner": "character", "status": "open", "text": "我答应记住这件事",
        "target_date": None, "source_turn_ids": ["s1"]}]
    result["reflections"] = [{"text": "现在回想，我觉得自己可以少一些急着给建议。", "source_turn_ids": ["s1"]}]
    document = DailyDocument.from_model_data(result, session_id="s1", memory_date=date(2026, 10, 30),
        source_session_ids=("s1",), allowed_turn_ids={"s1"})
    formal = document.render_text()
    review = document.render_text(include_evidence=True)
    assert "内部意向原话" not in formal and "用户想学习一种新技能" in formal
    assert "内部意向原话" in review
    assert "内部审核的证据原话" not in formal and "来源：" not in formal
    assert "内部审核的证据原话" in review and "来源：s1" in review
    assert "## 想做与打算" in formal and "## 明确承诺" in formal and "## 我的反思" in formal
    assert "uncertainties" not in document.to_data()
    assert DailyDocument.from_data(document.to_data()) == document


def test_commitment_is_not_a_valid_open_loop_certainty():
    result = daily_model_result()
    result["intentions"] = [{"owner": "user", "certainty": "commitment", "status": "open",
        "text": "明确承诺要单列", "target_date": None, "source_turn_ids": ["s1"], "evidence_quotes": ["明确承诺要单列"]}]
    with pytest.raises(InvalidStateError):
        DailyDocument.from_model_data(result, session_id="s1", memory_date=date(2026, 10, 30),
            source_session_ids=("s1",), allowed_turn_ids={"s1"})




async def generate_reviewable_job(repo):
    job_id, _ = await prepare_daily_attempt(repo)
    job = await repo.begin_generate(job_id)
    source = await repo.read_daily_source(job)
    doc = DailyDocument(source.plan.memory_date, ("s1",), "原标题",
        (DailyMoment(MomentKind.MILESTONE, "用户有一个进展。", ("s1",)),))
    candidate = DailyCandidate(1, doc, doc.content_hash(), datetime.now(timezone.utc))
    assert await repo.save_candidate(job_id, job.attempt_version, candidate, source=source)
    return job_id


def review_model_result(verdict="pass", document=None):
    return {"review_result": verdict, "issues": [] if verdict == "pass" else [{
        "severity": "minor", "type": "low_memory_value", "field": "memorable_moments",
        "description": "删除没有长期价值的泛泛进展", "source_turn_ids": ["s1"]}],
        "approved_daily": document, "review_summary": "保持原文" if verdict == "pass" else "删除噪声或重新提取"}


@pytest.mark.asyncio
async def test_begin_review_preserves_generation_checkpoint_and_recovery_rejects_late_review(store):
    from module.memory_management.daily.core.domain.review import DailyReview
    from module.memory_management.shared.contracts.review import ReviewVerdict
    _, repo = store
    job_id = await generate_reviewable_job(repo)
    ready = await repo.get_job(job_id)
    running = await repo.begin_review(job_id)
    assert running.status == MemoryJobStatus.REVIEWING
    assert running.attempt_version == ready.attempt_version + 1
    assert (running.candidate, running.plan, running.source_manifest) == (ready.candidate, ready.plan, ready.source_manifest)
    assert await repo.begin_review(job_id) is None
    assert await repo.recover_unfinished_attempts() == 1
    restored = await repo.get_job(job_id)
    assert restored.status == MemoryJobStatus.NEED_REVIEW and restored.candidate == ready.candidate
    passed = DailyReview(ReviewVerdict.PASS, (), None, "通过")
    assert not await repo.save_review(job_id, running.attempt_version, passed)
    restarted = await repo.begin_review(job_id)
    assert await repo.save_review(job_id, restarted.attempt_version, passed)
    assert await repo.save_review(job_id, restarted.attempt_version, passed)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["pass", "revised", "reject"])
async def test_review_model_to_sqlite_branch_preserves_original_candidate(store, verdict):
    from module.memory_management.daily.core.processing.prompts.review import REVIEW_PROMPT
    _, repo = store
    job_id = await generate_reviewable_job(repo)
    before = await repo.get_job(job_id)
    approved = replace(before.candidate.document, memorable_moments=()).to_data() if verdict == "revised" else None
    client = daily_model_client(review_model_result(verdict, approved))
    service = DailyProductionService(repo, model=DailyModelRepository(client), review_model=DailyModelRepository(client), prompt_resources=daily_character_resources())
    await service.process_job(job_id)
    saved = await repo.get_job(job_id)
    assert saved.candidate == before.candidate
    assert saved.review.review_result.value == verdict
    assert saved.reviewed_hash == before.candidate.content_hash
    assert saved.status == (MemoryJobStatus.NEED_GENERATE if verdict == "reject" else MemoryJobStatus.NEED_WRITE)
    if verdict == "revised":
        assert saved.review.approved_daily.memorable_moments == ()
    if verdict == "reject":
        assert saved.reextraction_count == 1 and "重新提取" in saved.last_error
    request = client.chat.completions.create.await_args.kwargs
    assert request["model"] == "deepseek-chat" and request["reasoning_effort"] == "low"
    assert request["messages"][0]["content"] == REVIEW_PROMPT
    assert json.loads(request["messages"][1]["content"])["CANDIDATE"] == before.candidate.document.to_data()


@pytest.mark.asyncio
async def test_review_rejection_is_bounded_and_feedback_reaches_regeneration(store):
    _, repo = store
    job_id = await generate_reviewable_job(repo)
    client = daily_model_client(review_model_result("reject"))
    service = DailyProductionService(repo, model=DailyModelRepository(client), review_model=DailyModelRepository(client), prompt_resources=daily_character_resources(),
        review_model_config=DailyModelConfig(max_reextractions=1))
    await service.process_job(job_id)
    rejected = await repo.get_job(job_id)
    client.chat.completions.create.return_value = daily_model_client(daily_model_result()).chat.completions.create.return_value
    await service.process_job(job_id)
    payload = json.loads(client.chat.completions.create.await_args.kwargs["messages"][1]["content"])
    assert "重新提取" in payload["PREVIOUS_ERRORS"]
    assert (await repo.get_job(job_id)).status == MemoryJobStatus.NEED_REVIEW
    client.chat.completions.create.return_value = daily_model_client(review_model_result("reject")).chat.completions.create.return_value
    await service.process_job(job_id)
    failed = await repo.get_job(job_id)
    assert failed.status == MemoryJobStatus.FAILED and failed.finished_at is not None
    assert failed.review is not None and failed.candidate is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "invalid_json", "changed_metadata", "invalid_ref"])
async def test_review_failure_retries_only_review_and_keeps_generated_checkpoint(store, failure):
    from datetime import timedelta
    _, repo = store
    job_id = await generate_reviewable_job(repo)
    before = await repo.get_job(job_id)
    result = review_model_result()
    if failure in ("changed_metadata", "invalid_ref"):
        body = before.candidate.document.to_data()
        if failure == "changed_metadata":
            body["source_session_ids"] = ["other"]
        else:
            body["profile_facts"] = [{"kind": "background", "text": "用户的背景",
                "source_turn_ids": ["unknown-turn"], "evidence_quotes": ["交给模型审核的原话"]}]
        result = review_model_result("revised", body)
    client = daily_model_client("invalid json" if failure == "invalid_json" else result)
    if failure == "timeout":
        client.chat.completions.create.side_effect = TimeoutError()
    service = DailyProductionService(repo, model=DailyModelRepository(client), review_model=DailyModelRepository(client), prompt_resources=daily_character_resources())
    workflow = DailyWorkflow(repo, service, retry_policy=RetryPolicy(max_retries=1, delays=(timedelta(0),)))
    assert await workflow.execute(job_id)
    saved = await repo.get_job(job_id)
    assert saved.status == MemoryJobStatus.NEED_REVIEW
    assert saved.candidate == before.candidate and saved.source_manifest == before.source_manifest
    assert saved.review is None and saved.retry_count == 1




@pytest.mark.asyncio
async def test_generation_and_review_use_separate_provider_connections(store):
    _, repo = store
    job_id, _ = await prepare_daily_attempt(repo)
    glm = daily_model_client(daily_model_result())
    ds = daily_model_client(review_model_result())
    service = DailyProductionService(repo, model=DailyModelRepository(glm), review_model=DailyModelRepository(ds),
        prompt_resources=daily_character_resources())
    await service.process_job(job_id)
    assert glm.chat.completions.create.await_args.kwargs["model"] == "glm-5.3"
    ds.chat.completions.create.assert_not_awaited()
    await service.process_job(job_id)
    assert glm.chat.completions.create.await_count == 1
    request = ds.chat.completions.create.await_args.kwargs
    assert request["model"] == "deepseek-chat" and request["reasoning_effort"] == "low"
    assert (await repo.get_job(job_id)).status == MemoryJobStatus.NEED_WRITE


@pytest.mark.asyncio
async def test_missing_review_client_does_not_send_deepseek_request_to_generation_provider(store):
    _, repo = store
    job_id = await generate_reviewable_job(repo)
    glm = daily_model_client(daily_model_result())
    service = DailyProductionService(repo, model=DailyModelRepository(glm), prompt_resources=daily_character_resources())
    with pytest.raises(NotImplementedError, match="审核模型"):
        await service.process_job(job_id)
    glm.chat.completions.create.assert_not_awaited()
    assert (await repo.get_job(job_id)).status == MemoryJobStatus.NEED_REVIEW


async def prepare_write_job(repo, *, revised=False):
    from module.memory_management.daily.core.domain.review import DailyReview
    from module.memory_management.shared.contracts.review import ReviewVerdict, ReviewSeverity
    from module.memory_management.daily.core.domain.review import ReviewIssue, DailyIssueType
    job_id = await generate_reviewable_job(repo)
    job = await repo.begin_review(job_id)
    approved = replace(job.candidate.document, title="审核后的标题") if revised else None
    issues = (ReviewIssue(ReviewSeverity.MINOR, DailyIssueType.UNCLEAR_WORDING, "title", "修正标题", ("s1",)),) if revised else ()
    review = DailyReview(ReviewVerdict.REVISED if revised else ReviewVerdict.PASS, issues, approved, "审核完成")
    assert await repo.save_review(job_id, job.attempt_version, review)
    return job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("revised", [False, True])
async def test_write_saves_structured_document_without_publishing(store, revised):
    database, repo = store
    job_id = await prepare_write_job(repo, revised=revised)
    before = await repo.get_job(job_id)
    await DailyProductionService(repo).process_job(job_id)
    saved = await repo.get_job(job_id)
    assert saved.status == MemoryJobStatus.NEED_INDEX
    assert saved.write_result.document == (before.review.approved_daily if revised else before.candidate.document)
    assert saved.write_result.document.render_text().startswith("# 审核后的标题" if revised else "# 原标题")
    assert "来源：" not in saved.write_result.document.render_text()
    assert saved.candidate == before.candidate and saved.review == before.review
    assert saved.result is saved.storage_job_id is None
    async with database.db_session() as session:
        row = await session.get(MemoryJobRow, job_id)
        assert "markdown" not in row.payload_json["write_result"]
        assert await session.scalar(select(func.count()).select_from(DailyMemoryRow)) == 0
    assert await repo.save_write(job_id, saved.attempt_version, saved.write_result)


@pytest.mark.asyncio
async def test_write_crash_recovers_without_regeneration_and_rejects_old_result(store):
    from module.memory_management.daily.core.domain.publication import DailyWrite
    _, repo = store
    job_id = await prepare_write_job(repo)
    running = await repo.begin_write(job_id)
    assert running.status == MemoryJobStatus.WRITING
    assert await repo.begin_write(job_id) is None
    result = DailyWrite.from_document(running.final_document())
    assert await repo.recover_unfinished_attempts() == 1
    ready = await repo.get_job(job_id)
    assert ready.status == MemoryJobStatus.NEED_WRITE and ready.write_result is None
    assert ready.candidate == running.candidate and ready.review == running.review
    assert not await repo.save_write(job_id, running.attempt_version, result)
    await DailyProductionService(repo).process_job(job_id)
    saved = await repo.get_job(job_id)
    assert saved.write_result == result and saved.status == MemoryJobStatus.NEED_INDEX
    assert await repo.recover_unfinished_attempts() == 0


@pytest.mark.asyncio
async def test_write_retry_keeps_model_checkpoints(store, monkeypatch):
    from datetime import timedelta
    from module.memory_management.daily.core.domain.publication import DailyWrite
    _, repo = store
    job_id = await prepare_write_job(repo)
    before = await repo.get_job(job_id)
    workflow = DailyWorkflow(repo, DailyProductionService(repo),
        retry_policy=RetryPolicy(max_retries=1, delays=(timedelta(0),)))
    def fail(*args):
        raise OSError("render interrupted")
    with monkeypatch.context() as patch:
        patch.setattr(DailyWrite, "from_document", fail)
        assert await workflow.execute(job_id)
    retry = await repo.get_job(job_id)
    assert retry.status == MemoryJobStatus.NEED_WRITE and retry.retry_count == 1
    assert retry.candidate == before.candidate and retry.review == before.review
    assert await workflow.execute(job_id)
    assert (await repo.get_job(job_id)).status == MemoryJobStatus.NEED_INDEX


@pytest.mark.asyncio
async def test_pending_index_blocks_later_same_date_job(store):
    _, repo = store
    job_id = await prepare_write_job(repo)
    await DailyProductionService(repo).process_job(job_id)
    later_id = await repo.create_job(command(sid="s2"))
    selected = await repo.get_next_ready_job()
    assert selected.job_id == job_id and selected.status == MemoryJobStatus.NEED_INDEX
    assert selected.job_id != later_id
    assert await repo.begin_generate(later_id) is None


@pytest.mark.asyncio
async def test_write_rejects_unreviewed_document_and_wrong_review_binding(store):
    from module.memory_management.daily.core.domain.publication import DailyWrite
    _, repo = store
    job_id = await prepare_write_job(repo, revised=True)
    running = await repo.begin_write(job_id)
    with pytest.raises(InvalidStateError, match="审核结果不一致"):
        await repo.save_write(job_id, running.attempt_version,
            DailyWrite.from_document(running.candidate.document))
    with pytest.raises(InvalidStateError, match="未绑定"):
        replace(running, reviewed_hash="wrong").final_document()


async def prepare_index_job(repo):
    job_id = await prepare_write_job(repo)
    await DailyProductionService(repo).process_job(job_id)
    return job_id


@pytest.mark.asyncio
async def test_service_indexes_before_sqlite_progress_and_creates_no_storage_job(store):
    _, repo = store
    job_id = await prepare_index_job(repo)
    class Index:
        async def batch_hybrid_index(self,document,representations):
            current = await repo.get_job(job_id)
            assert current.status == MemoryJobStatus.INDEXING
            assert current.vector_document is None and current.storage_job_id is None
            assert [item.section_key for item in representations] == ["memorable_moments"]
            return True
    await DailyProductionService(repo,index_repository=Index()).process_job(job_id)
    saved=await repo.get_job(job_id)
    assert saved.status==MemoryJobStatus.NEED_ACTIVATE
    assert saved.vector_document.ref==saved.index_document().ref
    assert saved.storage_job_id is None and saved.result is None
    assert await repo.save_index(job_id,saved.attempt_version,saved.vector_document)


@pytest.mark.asyncio
async def test_index_crash_restores_same_document_and_refuses_late_completion(store):
    _, repo=store
    job_id=await prepare_index_job(repo)
    running=await repo.begin_index(job_id)
    document=running.index_document()
    assert await repo.begin_index(job_id) is None
    assert await repo.recover_unfinished_attempts()==1
    restored=await repo.get_job(job_id)
    assert restored.status==MemoryJobStatus.NEED_INDEX
    assert restored.index_document()==document and restored.write_result==running.write_result
    assert not await repo.save_index(job_id,running.attempt_version,document)
    restarted=await repo.begin_index(job_id)
    assert await repo.save_index(job_id,restarted.attempt_version,document)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["index", "sqlite"])
async def test_index_retry_reencodes_after_partial_success_and_preserves_write(store,monkeypatch,failure):
    from datetime import timedelta
    _,repo=store
    job_id=await prepare_index_job(repo)
    documents=[]
    class Index:
        async def batch_hybrid_index(self,document,representations):
            documents.append(document)
            if failure=="index" and len(documents)==1:
                return False
            return True
    service=DailyProductionService(repo,index_repository=Index())
    workflow=DailyWorkflow(repo,service,retry_policy=RetryPolicy(max_retries=1,delays=(timedelta(0),)))
    if failure=="sqlite":
        original=repo.save_index
        async def fail_once(*args,**kwargs):
            raise OSError("commit interrupted")
        with monkeypatch.context() as patch:
            patch.setattr(repo,"save_index",fail_once)
            assert await workflow.execute(job_id)
    else:
        assert await workflow.execute(job_id)
    pending=await repo.get_job(job_id)
    assert pending.status==MemoryJobStatus.NEED_INDEX and pending.write_result is not None
    assert await workflow.execute(job_id)
    saved=await repo.get_job(job_id)
    assert saved.status==MemoryJobStatus.NEED_ACTIVATE
    assert len(documents)==2 and documents[0]==documents[1]


async def prepare_activation_job(repo):
    job_id=await prepare_index_job(repo)
    running=await repo.begin_index(job_id)
    assert await repo.save_index(job_id,running.attempt_version,running.index_document())
    return job_id


@pytest.mark.asyncio
async def test_activation_commits_json_and_done_without_files(store, tmp_path):
    from pathlib import Path
    database,repo=store
    job_id=await prepare_activation_job(repo)
    before=await repo.get_job(job_id)
    await DailyProductionService(repo).process_job(job_id)
    done=await repo.get_job(job_id)
    assert done.status==MemoryJobStatus.DONE and done.finished_at is not None
    assert done.result==before.vector_document.ref and done.applied_revision==1
    async with database.db_session() as session:
        daily=await session.get(DailyMemoryRow,done.result.memory_id)
        assert daily.revision==1 and daily.document_json==before.write_result.document.to_data()
        assert daily.last_mentioned_at == daily.published_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    assert not list(tmp_path.rglob("*.md"))
    # Completed publication is idempotent.
    assert await repo.activate_revision(job_id,done.attempt_version)
    assert await repo.get_job(job_id) == done


@pytest.mark.asyncio
async def test_activation_sql_failure_rolls_back_formal_document_and_done(store):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    database, repo = store
    job_id = await prepare_activation_job(repo)
    job = await repo.get_job(job_id)
    async with database.transaction() as session:
        await session.execute(text("CREATE TRIGGER reject_done BEFORE UPDATE ON memory_jobs WHEN NEW.status='done' BEGIN SELECT RAISE(ABORT, 'test failure'); END;"))
    with pytest.raises(IntegrityError):
        await repo.activate_revision(job_id, job.attempt_version)
    async with database.db_session() as session:
        assert await session.get(DailyMemoryRow, job.vector_document.ref.memory_id) is None
    assert (await repo.get_job(job_id)).status == MemoryJobStatus.NEED_ACTIVATE
    assert (await repo.get_job(job_id)).result is None
    async with database.transaction() as session:
        await session.execute(text("DROP TRIGGER reject_done"))
    assert await repo.activate_revision(job_id, job.attempt_version)
    assert (await repo.get_job(job_id)).result.revision == 1


@pytest.mark.asyncio
async def test_activation_rejects_changed_base(store):
    database,repo=store
    job_id=await prepare_activation_job(repo)
    job=await repo.get_job(job_id)
    ref=job.vector_document.ref
    now=datetime.now(timezone.utc)
    async with database.transaction() as session:
        session.add(DailyMemoryRow(daily_id=ref.memory_id,character_id="c",memory_date=job.input.memory_date,
            source_session_ids_json=["other"],document_json=job.write_result.document.to_data(),
            revision=8,content_hash="newer",vector_document_key="other",published_at=now,
            recall_tier="hot",created_at=now,updated_at=now))
    with pytest.raises(MemorySourceChangedError):
        await repo.activate_revision(job_id,job.attempt_version)
    assert (await repo.get_job(job_id)).result is None


@pytest.mark.asyncio
async def test_next_session_updates_same_daily_once(store):
    from pathlib import Path
    from module.memory_management.daily.core.domain.review import DailyReview
    from module.memory_management.shared.contracts.review import ReviewVerdict
    database,repo=store
    first_id=await prepare_activation_job(repo)
    await DailyProductionService(repo).process_job(first_id)
    first=await repo.get_job(first_id)
    second_id=await repo.create_job(command(sid="s2"))
    running=await repo.begin_generate(second_id)
    source=await repo.read_daily_source(running)
    doc=replace(source.base_daily,title="第二个session补充",source_session_ids=("s1","s2"))
    candidate=DailyCandidate(1,doc,doc.content_hash(),datetime.now(timezone.utc))
    assert await repo.save_candidate(second_id,running.attempt_version,candidate,source=source)
    reviewing=await repo.begin_review(second_id)
    assert await repo.save_review(second_id,reviewing.attempt_version,DailyReview(ReviewVerdict.PASS,(),None,"通过"))
    await DailyProductionService(repo).process_job(second_id)
    indexing=await repo.begin_index(second_id)
    assert await repo.save_index(second_id,indexing.attempt_version,indexing.index_document())
    await DailyProductionService(repo).process_job(second_id)
    second=await repo.get_job(second_id)
    assert second.status==MemoryJobStatus.DONE
    assert second.result.memory_id==first.result.memory_id and second.result.revision==2
    assert second.write_result.document.title == "第二个session补充"
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(DailyMemoryRow))==1
        row=await session.get(DailyMemoryRow,second.result.memory_id)
        assert row.source_session_ids_json==["s1","s2"] and row.revision==2
