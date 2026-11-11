"""Object任务登记：封存扫描、幂等、跨链独立及中断后的继续登记。"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from module.shared.common.errors.base import IdempotencyConflictError, InvalidStateError, NotFoundError
from module.shared.context.types.references import InheritedContextRef
from module.memory_management.object.core.processing.service import ObjectProductionService
from module.memory_management.shared.jobs.core.domain.command import CreateMemoryJob
from module.memory_management.shared.jobs.core.domain.job import MemoryJobStatus
from module.memory_management.object.core.domain.job import ObjectJob
from module.memory_management.object.core.domain.source import ObjectSourceInput
from module.memory_management.shared.jobs.repository import mappers as job_mappers
from module.memory_management.object.repository import mappers as object_mappers
from storage.sqlite.value_codecs import SQLiteValueCodec
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow, ConversationRow, SessionRow, MemoryJobRow, MessageRow, TurnRow
from module.memory_management.object.repository.sqlite import ObjectProductionRepository
from module.memory_management.object.core.domain.job import ObjectJobInput
from module.memory_management.object.repository.llm import ObjectModelRepository


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(tmp_path / "object.sqlite3")
    await database.initialize()
    now = datetime.now(timezone.utc)
    try:
        async with database.transaction() as session:
            session.add_all(CharacterRow(character_id=cid, name=cid, character_setting="test")
                            for cid in ("c", "other"))
            await session.flush()
            session.add_all(ConversationRow(conversation_id=conv, character_id=cid, state="active",
                created_at=now, updated_at=now) for conv, cid in (("conv", "c"), ("conv2", "c"), ("conv3", "other")))
            await session.flush()
            for seq, (sid, conv, state) in enumerate((
                ("s1", "conv", "sealed"), ("s2", "conv", "sealed"),
                ("open", "conv", "open"), ("waiting", "conv2", "waiting_handoff"),
                ("s3", "conv3", "sealed"),
            )):
                session.add(SessionRow(session_id=sid, conversation_id=conv, session_seq=seq + 1, session_state=state,
                    session_start_time=now, session_end_time=now + timedelta(seconds=seq) if state == "sealed" else None))
        yield database, ObjectProductionRepository(database)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_registration_persists_checkpoint_and_idempotent_replay(store):
    database, repo = store
    command = ObjectJobInput.create_command("c", "s1")
    job_id = await repo.create_job(command)
    job = await repo.get_job(job_id)
    assert isinstance(job, ObjectJob) and job.input == command.input
    assert job.status == MemoryJobStatus.NEED_GENERATE
    assert job.target_key == "object:c:s1" and job.dedupe_key == "extract_objects:c:s1"
    assert job.attempt_count == job.attempt_version == 0 and job.source_manifest is None
    async with database.transaction() as session:
        row = await session.get(MemoryJobRow, job_id)
        finished = replace(job, status=MemoryJobStatus.DONE, attempt_version=3, attempt_count=2)
        row.status = finished.status.value
        row.payload_json = job_mappers.memory_job_payload_to_json(finished)
    assert await repo.create_job(command) == job_id
    assert await repo.get_job(job_id) == finished
    with pytest.raises(IdempotencyConflictError):
        await repo.create_job(replace(command, priority=20))
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 1


@pytest.mark.asyncio
async def test_concurrent_registration_creates_one_job(store):
    database, repo = store
    command = ObjectJobInput.create_command("c", "s1")
    first, second = await asyncio.gather(repo.create_job(command), repo.create_job(command))
    assert first == second
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [MemoryJobStatus.NEED_GENERATE, MemoryJobStatus.GENERATING,
                                   MemoryJobStatus.DONE, MemoryJobStatus.FAILED])
async def test_scan_excludes_registered_sessions_in_any_stage_and_ignores_daily_marker(store, status):
    database, repo = store
    async with database.transaction() as session:
        (await session.get(SessionRow, "s2")).daily_jobs_registered_at = datetime.now(timezone.utc)
    assert await repo.get_unregister_sealed_sessions(1) == ["s1"]
    job_id = await repo.create_job(ObjectJobInput.create_command("c", "s1"))
    async with database.transaction() as session:
        row = await session.get(MemoryJobRow, job_id)
        job = replace(job_mappers.memory_job_to_domain(row), status=status)
        row.status, row.payload_json = status.value, job_mappers.memory_job_payload_to_json(job)
    assert await repo.get_unregister_sealed_sessions() == ["s2", "s3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command,error", [
    (ObjectJobInput.create_command("c", "missing"), NotFoundError),
    (ObjectJobInput.create_command("c", "open"), InvalidStateError),
    (ObjectJobInput.create_command("c", "waiting"), InvalidStateError),
    (ObjectJobInput.create_command("other", "s1"), InvalidStateError),
    (replace(ObjectJobInput.create_command("c", "s1"), target_key="wrong"), InvalidStateError),
    (replace(ObjectJobInput.create_command("c", "s1"), dedupe_key="wrong"), InvalidStateError),
])
async def test_invalid_registration_does_not_write_or_hide_session(store, command, error):
    database, repo = store
    with pytest.raises(error):
        await repo.create_job(command)
    async with database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 0
    assert (await repo.get_unregister_sealed_sessions())[0] == "s1"


@pytest.mark.asyncio
async def test_service_registers_each_session_once_without_loading_dialogue(store):
    database, repo = store
    service = ObjectProductionService(repo)
    ids = await service.register_object_jobs(priority=7)
    assert len(set(ids)) == 3
    assert await service.register_object_jobs() == ()
    jobs = [await repo.get_job(job_id) for job_id in ids]
    assert [job.input.source_session_ids for job in jobs] == [("s1",), ("s2",), ("s3",)]
    assert all(job.priority == 7 for job in jobs)
    assert len({job.target_key for job in jobs}) == 3
    async with database.db_session() as session:
        assert (await session.get(SessionRow, "s1")).daily_jobs_registered_at is None


@pytest.mark.asyncio
async def test_failed_registration_is_retried_and_does_not_block_other_sessions(store, monkeypatch):
    _, repo = store
    original = repo.create_job

    async def fail_first(command):
        if command.input.source_session_ids == ("s1",):
            raise OSError("write interrupted")
        return await original(command)

    monkeypatch.setattr(repo, "create_job", fail_first)
    service = ObjectProductionService(repo)
    assert len(await service.register_object_jobs()) == 2
    assert await repo.get_unregister_sealed_sessions() == ["s1"]
    monkeypatch.setattr(repo, "create_job", original)
    assert len(await service.register_object_jobs()) == 1
    assert await repo.get_unregister_sealed_sessions() == []


@pytest.mark.asyncio
async def test_begin_generate_is_atomic_and_does_not_load_source(store, monkeypatch):
    _, repo = store
    job_id = await repo.create_job(ObjectJobInput.create_command("c", "s1"))

    async def forbidden(*args):
        raise AssertionError("begin_generate不应加载原文")

    monkeypatch.setattr(repo, "_read_sealed_session_source", forbidden)
    results = await asyncio.gather(repo.begin_generate(job_id), repo.begin_generate(job_id))
    assert sum(result is not None for result in results) == 1
    started = next(result for result in results if result is not None)
    assert started.status == MemoryJobStatus.GENERATING
    assert started.attempt_version == started.attempt_count == 1
    assert started.source_manifest is None and started.objects == ()
    assert await repo.get_job(job_id) == started


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [MemoryJobStatus.NEED_INDEX, MemoryJobStatus.DONE, MemoryJobStatus.FAILED])
async def test_begin_generate_does_not_rewind_saved_stages(store, status):
    database, repo = store
    job_id = await repo.create_job(ObjectJobInput.create_command("c", "s1"))
    original = await repo.get_job(job_id)
    current = replace(original, status=status)
    async with database.transaction() as session:
        row = await session.get(MemoryJobRow, job_id)
        row.status, row.payload_json = status.value, job_mappers.memory_job_payload_to_json(current)
    assert await repo.begin_generate(job_id) is None
    assert await repo.get_job(job_id) == current


@pytest.mark.asyncio
async def test_delayed_head_blocks_same_target_until_terminal(store):
    database, repo = store
    job_id = await repo.create_job(ObjectJobInput.create_command("c", "s1"))
    original = await repo.get_job(job_id)
    head = replace(original, available_at=datetime.now(timezone.utc) + timedelta(hours=1))
    tail = replace(original, job_id="tail", dedupe_key="tail", created_at=original.created_at + timedelta(seconds=1))
    async with database.transaction() as session:
        row = await session.get(MemoryJobRow, job_id)
        row.available_at, row.payload_json = head.available_at, job_mappers.memory_job_payload_to_json(head)
        session.add(job_mappers.memory_job_to_row(tail))
    assert await repo.begin_generate(job_id) is None
    assert await repo.begin_generate("tail") is None
    async with database.transaction() as session:
        row = await session.get(MemoryJobRow, job_id)
        row.status = MemoryJobStatus.DONE.value
    assert (await repo.begin_generate("tail")).status == MemoryJobStatus.GENERATING


@pytest.mark.asyncio
async def test_begin_generate_rejects_missing_and_other_family(store):
    database, repo = store
    with pytest.raises(NotFoundError):
        await repo.begin_generate("missing")
    from module.memory_management.daily.core.domain.job import DailyJob, DailyJobInput
    from module.memory_management.shared.jobs.core.domain.job import MemoryJobType
    now = datetime.now(timezone.utc)
    daily = DailyJob("daily", MemoryJobType.CREATE_DAILY, "daily:c:date", "daily", 10,
        MemoryJobStatus.NEED_GENERATE, now, DailyJobInput("c", ("s1",)), now, now)
    async with database.transaction() as session:
        session.add(job_mappers.memory_job_to_row(daily))
    with pytest.raises(InvalidStateError):
        await repo.begin_generate("daily")


@pytest.mark.asyncio
async def test_object_source_keeps_all_dates_and_only_accepted_replies(store):
    database, repo = store
    job_id = await repo.create_job(ObjectJobInput.create_command("c", "s1"))
    job = await repo.begin_generate(job_id)
    before = datetime(2026, 10, 30, 15, 59, tzinfo=timezone.utc)
    after = before + timedelta(minutes=3)
    async with database.transaction() as session:
        for seq, created_at in ((1, before), (2, after)):
            tid = f"t{seq}"
            session.add(TurnRow(turn_id=tid, session_id="s1", turn_seq=seq, turn_state="complete"))
            await session.flush()
            session.add_all([
                MessageRow(message_id=tid + "u", turn_id=tid, message_seq=1, role="user", content="用户原文", created_at=created_at),
                MessageRow(message_id=tid + "unused", turn_id=tid, message_seq=2, role="character", content="未采用的回复", created_at=created_at),
                MessageRow(message_id=tid + "a", turn_id=tid, message_seq=3, role="character", content="正式回复", created_at=created_at),
            ])
            await session.flush()
            (await session.get(TurnRow, tid)).accepted_message_id = tid + "a"
    source = await repo.read_object_source(job)
    assert isinstance(source, ObjectSourceInput)
    assert (source.session_id, source.character_id) == ("s1", "c")
    assert [turn.turn_id for turn in source.owned_turns] == ["t1", "t2"]
    assert [turn.accepted_reply.message_id for turn in source.owned_turns] == ["t1a", "t2a"]
    assert [turn.user_messages[0].created_at for turn in source.owned_turns] == [before, after]
    assert (await repo.read_sealed_session_source("s1")).turns == source.owned_turns
    payload = source.to_model_payload()
    assert payload["session_id"] == "s1"
    assert [message["message_id"] for message in payload["messages"]] == ["t1u", "t1a", "t2u", "t2a"]
    assert [message["role"] for message in payload["messages"]] == ["user", "assistant", "user", "assistant"]
    assert [message["created_at"] for message in payload["messages"]] == [
        "2026-10-30T15:59:00+00:00", "2026-10-30T15:59:00+00:00",
        "2026-10-30T16:02:00+00:00", "2026-10-30T16:02:00+00:00",
    ]
    assert await repo.get_job(job_id) == job
    with pytest.raises(InvalidStateError):
        await repo.read_object_source(replace(job, input=replace(job.input, character_id="other")))

    # 下一个session继承前驱原文，但这些消息不成为本session拥有的事实来源。
    inherited = InheritedContextRef.handoff("s1", last_turn_seq=2)
    async with database.transaction() as session:
        row = await session.get(SessionRow, "s2")
        row.predecessor_session_id = "s1"
        row.inherited_context_json = SQLiteValueCodec.inherited_context_to_json(inherited)
    next_id = await repo.create_job(ObjectJobInput.create_command("c", "s2"))
    next_source = await repo.read_object_source(await repo.get_job(next_id))
    assert next_source.owned_turns == ()
    assert next_source.inherited_context.ref == inherited
    assert next_source.inherited_context.complete_turns == source.owned_turns
    next_payload = next_source.to_model_payload()
    assert next_payload["messages"] == []
    assert len(next_payload["INHERITED_CONTEXT"]["complete_turns"]) == 2
    async with database.transaction() as session:
        (await session.get(SessionRow, "s2")).inherited_context_json = None
    with pytest.raises(InvalidStateError, match="固定继承材料"):
        await repo.read_object_source(await repo.get_job(next_id))


@pytest.mark.asyncio
async def test_object_source_rejects_unsealed_and_incomplete_sessions(store):
    database, repo = store
    job_id = await repo.create_job(ObjectJobInput.create_command("c", "s1"))
    job = await repo.get_job(job_id)
    assert (await repo.read_object_source(job)).owned_turns == ()
    with pytest.raises(InvalidStateError):
        await repo.read_object_source(replace(job, input=replace(job.input, source_session_ids=("open",))))
    with pytest.raises(NotFoundError):
        await repo.read_object_source(replace(job, input=replace(job.input, source_session_ids=("missing",))))
    async with database.transaction() as session:
        session.add(TurnRow(turn_id="incomplete", session_id="s1", turn_seq=1, turn_state="current"))
    with pytest.raises(InvalidStateError, match="accepted reply"):
        await repo.read_object_source(job)


@pytest_asyncio.fixture
async def production_case(store):
    """真实SQLite来源，模型和索引调用可按故障点控制。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import json
    database, repo = store
    now = datetime(2026, 10, 31, 8, tzinfo=timezone.utc)
    async with database.transaction() as session:
        session.add(TurnRow(turn_id="t", session_id="s1", turn_seq=1, turn_state="complete"))
        await session.flush()
        session.add_all([
            MessageRow(message_id="u", turn_id="t", message_seq=1, role="user",
                       content="我决定明天开始整理项目", created_at=now),
            MessageRow(message_id="a", turn_id="t", message_seq=2, role="character",
                       content="好，陪你一起整理", created_at=now),
        ])
        await session.flush()
        (await session.get(TurnRow, "t")).accepted_message_id = "a"
    data = {"session_id": "s1", "selection_notes": [
        {"item": "整理项目", "decision": "keep", "reason": "有明确行动", "message_ids": ["u"]}],
        "objects": [{"object": "项目整理", "events": [], "plans": [
            {"actor": "user", "statement": "决定明天开始整理项目", "acquire_time": "2026-10-31",
             "target_time": "2026-11-01", "message_ids": ["u"]}], "forgetfulness": 0}]}
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(data, ensure_ascii=False)))])
    create = AsyncMock(return_value=response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    index = SimpleNamespace(batch_object_index=AsyncMock(return_value=True))
    service = ObjectProductionService(repo, model=ObjectModelRepository(client), index_repository=index)
    job_id = await repo.create_job(ObjectJobInput.create_command("c", "s1"))
    return repo, service, create, index, job_id, response, data


@pytest.mark.asyncio
async def test_generate_save_and_index_preserve_source_and_do_not_repeat_model(production_case):
    import json
    repo, service, create, index, job_id, _, _ = production_case
    await service.process_job(job_id)
    saved = await repo.get_job(job_id)
    assert saved.status == MemoryJobStatus.NEED_INDEX
    assert saved.source_manifest.source_message_ids == ("u", "a")
    candidate = saved.objects[0]
    assert candidate.plans[0].target_time.isoformat() == "2026-11-01"
    assert candidate.plans[0].source_refs[0].quote == "我决定明天开始整理项目"
    assert saved.selection_notes[0].message_ids == ("u",)
    request = create.call_args.kwargs
    assert request["reasoning_effort"] == service._model_config.reasoning_effort
    assert request["response_format"] == {"type": "json_object"}
    schema = json.loads(request["messages"][0]["content"].split("OUTPUT_SCHEMA的JSON对象：\n", 1)[1])
    assert schema["properties"]["session_id"]["enum"] == ["s1"]
    payload = json.loads(request["messages"][1]["content"].split("\n", 1)[1])
    assert payload["session_id"] == "s1" and "INHERITED_CONTEXT" in payload
    source = await repo.read_object_source(saved)
    assert await repo.save_extraction_result(job_id, saved.attempt_version, saved.objects,
        source=source, selection_notes=saved.selection_notes)
    assert not await repo.save_extraction_result(job_id, saved.attempt_version - 1, saved.objects, source=source)
    assert await repo.begin_generate(job_id) is None
    await service.process_job(job_id)
    indexed = await repo.get_job(job_id)
    assert indexed.status == MemoryJobStatus.NEED_ACTIVATE
    assert indexed.objects == saved.objects
    assert indexed.attempt_version == saved.attempt_version + 1
    create.assert_awaited_once()
    index.batch_object_index.assert_awaited_once_with(saved.objects)
    assert await repo.save_index(job_id, indexed.attempt_version)
    assert not await repo.save_index(job_id, saved.attempt_version)
    assert await repo.begin_index(job_id) is None


@pytest.mark.asyncio
async def test_index_failure_and_crash_recovery_reuse_saved_candidates(production_case):
    from module.memory_management.shared.contracts.errors import MemoryIndexUnavailable
    repo, service, create, index, job_id, _, _ = production_case
    await service.process_job(job_id)
    saved = await repo.get_job(job_id)
    index.batch_object_index.return_value = False
    with pytest.raises(MemoryIndexUnavailable):
        await service.process_job(job_id)
    interrupted = await repo.get_job(job_id)
    assert interrupted.status == MemoryJobStatus.INDEXING
    assert await repo.recover_unfinished_attempts() == 1
    recovered = await repo.get_job(job_id)
    assert recovered.status == MemoryJobStatus.NEED_INDEX
    assert recovered.objects == saved.objects
    # 恢复使旧代次失效，迟到的索引结果不能推进状态。
    assert not await repo.save_index(job_id, interrupted.attempt_version)
    index.batch_object_index.return_value = True
    await service.process_job(job_id)
    assert (await repo.get_job(job_id)).status == MemoryJobStatus.NEED_ACTIVATE
    create.assert_awaited_once()
    assert index.batch_object_index.await_count == 2


@pytest.mark.asyncio
async def test_begin_index_is_atomic(production_case):
    repo, service, _, _, job_id, _, _ = production_case
    await service.process_job(job_id)
    results = await asyncio.gather(repo.begin_index(job_id), repo.begin_index(job_id))
    assert sum(result is not None for result in results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["truncated", "bad_json", "empty", "whitespace", "null_content", "multiple_json", "foreign_reference", "bad_date", "network"])
async def test_bad_model_result_never_becomes_indexable(production_case, failure):
    import json
    from module.memory_management.shared.contracts.errors import MemoryModelUnavailable
    repo, service, create, _, job_id, response, data = production_case
    if failure == "truncated":
        response.choices[0].finish_reason = "length"
    elif failure == "bad_json":
        response.choices[0].message.content = "{"
    elif failure in ("empty", "whitespace", "null_content", "multiple_json"):
        response.choices[0].message.content = {"empty": "", "whitespace": "   \n ",
            "null_content": None, "multiple_json": "{}\n{}"}[failure]
    elif failure == "network":
        create.side_effect = TimeoutError()
    else:
        entry = data["objects"][0]["plans"][0]
        if failure == "foreign_reference":
            entry["message_ids"] = ["previous-session-message"]
        else:
            entry["target_time"] = "2026-02-30"
        response.choices[0].message.content = json.dumps(data)
    with pytest.raises(MemoryModelUnavailable) as error:
        await service.process_job(job_id)
    assert error.value.retryable is True
    failed = await repo.get_job(job_id)
    assert failed.status == MemoryJobStatus.GENERATING
    assert failed.objects == () and failed.source_manifest is None
    assert await repo.recover_unfinished_attempts() == 1
    assert (await repo.get_job(job_id)).status == MemoryJobStatus.NEED_GENERATE


@pytest.mark.asyncio
async def test_empty_extraction_is_saved_and_can_complete_index_stage(production_case):
    repo, service, _, index, job_id, response, _ = production_case
    response.choices[0].message.content = '{"session_id":"s1","objects":[],"selection_notes":[]}'
    await service.process_job(job_id)
    saved = await repo.get_job(job_id)
    assert saved.status == MemoryJobStatus.NEED_INDEX and saved.objects == ()
    assert saved.source_manifest is not None
    await service.process_job(job_id)
    assert (await repo.get_job(job_id)).status == MemoryJobStatus.NEED_ACTIVATE
    index.batch_object_index.assert_awaited_once_with(())


@pytest.mark.asyncio
async def test_source_changed_during_model_call_does_not_save_stale_candidate(production_case):
    from module.memory_management.shared.contracts.errors import MemorySourceChangedError
    repo, service, create, _, job_id, response, _ = production_case

    async def change_source(**kwargs):
        async with repo._database.transaction() as session:
            (await session.get(MessageRow, "u")).content = "我取消整理计划"
        return response

    create.side_effect = change_source
    with pytest.raises(MemorySourceChangedError):
        await service.process_job(job_id)
    job = await repo.get_job(job_id)
    assert job.status == MemoryJobStatus.GENERATING and job.source_manifest is None
    assert job.objects == ()


@pytest.mark.asyncio
async def test_activation_publishes_batch_atomically_and_replay_preserves_topic_state(production_case):
    from module.memory_management.object.core.domain.document import ObjectState
    from storage.sqlite.models import MemoryObjectRow
    repo, service, create, index, job_id, _, _ = production_case
    await service.process_job(job_id)
    saved = await repo.get_job(job_id)
    async with repo._database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryObjectRow)) == 0
    await service.process_job(job_id)
    indexed = await repo.get_job(job_id)
    async with repo._database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryObjectRow)) == 0
    await service.process_job(job_id)
    done = await repo.get_job(job_id)
    assert done.status == MemoryJobStatus.DONE
    assert done.finished_at is not None and done.updated_at == done.finished_at
    assert done.objects == saved.objects
    assert done.attempt_version == indexed.attempt_version
    async with repo._database.db_session() as session:
        row = await session.get(MemoryObjectRow, done.objects[0].object_id)
        assert object_mappers.memory_object_to_domain(row) == done.objects[0]
        assert row.object_state == "staged"  # 已发布，等待topic整合
    # topic后续改为linked后，重复激活不能恢复成staged。
    async with repo._database.transaction() as session:
        row = await session.get(MemoryObjectRow, done.objects[0].object_id)
        row.object_state, row.linked_topic_id = ObjectState.LINKED.value, "future-topic"
    assert await repo.activate_revision(job_id, done.attempt_version)
    assert not await repo.activate_revision(job_id, done.attempt_version - 1)
    await service.process_job(job_id)
    assert await repo.get_job(job_id) == done
    async with repo._database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryObjectRow)) == 1
        row = await session.get(MemoryObjectRow, done.objects[0].object_id)
        assert row.object_state == "linked" and row.linked_topic_id == "future-topic"
    create.assert_awaited_once()
    index.batch_object_index.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_extraction_activates_without_creating_objects(production_case):
    from storage.sqlite.models import MemoryObjectRow
    repo, service, _, _, job_id, response, _ = production_case
    response.choices[0].message.content = '{"session_id":"s1","objects":[],"selection_notes":[]}'
    for _ in range(3):
        await service.process_job(job_id)
    assert (await repo.get_job(job_id)).status == MemoryJobStatus.DONE
    async with repo._database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryObjectRow)) == 0


@pytest.mark.asyncio
async def test_activation_flush_failure_rolls_back_rows_and_done_together(production_case, monkeypatch):
    import json
    from sqlalchemy.ext.asyncio import AsyncSession
    from storage.sqlite.models import MemoryObjectRow
    repo, service, _, _, job_id, response, data = production_case
    other = dict(data["objects"][0], object="另一个项目")
    data["objects"].append(other)
    response.choices[0].message.content = json.dumps(data)
    await service.process_job(job_id)
    await service.process_job(job_id)
    indexed = await repo.get_job(job_id)
    original = AsyncSession.flush

    async def crash_after_flush(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise OSError("simulated crash before commit")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "flush", crash_after_flush)
        with pytest.raises(OSError):
            await service.process_job(job_id)
    assert await repo.get_job(job_id) == indexed
    async with repo._database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryObjectRow)) == 0
    await service.process_job(job_id)
    assert (await repo.get_job(job_id)).status == MemoryJobStatus.DONE
    async with repo._database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryObjectRow)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["id", "source_position"])
async def test_activation_conflict_never_overwrites_existing_object(production_case, collision):
    from storage.sqlite.models import MemoryObjectRow
    repo, service, _, _, job_id, _, _ = production_case
    await service.process_job(job_id)
    await service.process_job(job_id)
    indexed = await repo.get_job(job_id)
    candidate = indexed.objects[0]
    previous = (replace(candidate, object="已有的不同内容") if collision == "id"
                else replace(candidate, object_id="another-id"))
    async with repo._database.transaction() as session:
        session.add(object_mappers.memory_object_to_row(previous))
    with pytest.raises(IdempotencyConflictError):
        await service.process_job(job_id)
    assert await repo.get_job(job_id) == indexed
    async with repo._database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryObjectRow)) == 1
        assert object_mappers.memory_object_to_domain(await session.get(MemoryObjectRow, previous.object_id)) == previous


@pytest.mark.asyncio
async def test_activation_reuses_identical_existing_row_and_concurrent_calls(production_case):
    from storage.sqlite.models import MemoryObjectRow
    repo, service, _, _, job_id, _, _ = production_case
    await service.process_job(job_id)
    await service.process_job(job_id)
    indexed = await repo.get_job(job_id)
    async with repo._database.transaction() as session:
        session.add(object_mappers.memory_object_to_row(indexed.objects[0]))
    results = await asyncio.gather(*(repo.activate_revision(job_id, indexed.attempt_version) for _ in range(2)))
    assert results == [True, True]
    assert (await repo.get_job(job_id)).status == MemoryJobStatus.DONE
    async with repo._database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryObjectRow)) == 1


@pytest.mark.asyncio
async def test_activation_respects_stage_backoff_and_attempt_version(production_case):
    from storage.sqlite.models import MemoryObjectRow
    repo, service, _, _, job_id, _, _ = production_case
    await service.process_job(job_id)
    saved = await repo.get_job(job_id)
    assert not await repo.activate_revision(job_id, saved.attempt_version)
    await service.process_job(job_id)
    indexed = await repo.get_job(job_id)
    assert not await repo.activate_revision(job_id, indexed.attempt_version - 1)
    async with repo._database.transaction() as session:
        (await session.get(MemoryJobRow, job_id)).available_at = datetime.now(timezone.utc) + timedelta(hours=1)
    assert not await repo.activate_revision(job_id, indexed.attempt_version)
    async with repo._database.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryObjectRow)) == 0
    with pytest.raises(NotFoundError):
        await repo.activate_revision("missing", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("state,tier,topic", [("staged", "hot", None), ("linked", "cold", "topic"), ("discarded", "archived", None)])
async def test_formal_object_mapping_preserves_events_plans_evidence_and_lifecycle(production_case, state, tier, topic):
    from module.memory_management.object.core.domain.document import ObjectState, ObjectEvent
    from module.shared.memory.types.retention import RecallTier
    from storage.sqlite.models import MemoryObjectRow
    repo, service, _, _, job_id, _, _ = production_case
    await service.process_job(job_id)
    candidate = (await repo.get_job(job_id)).objects[0]
    plan = candidate.plans[0]
    candidate = replace(candidate, events=(ObjectEvent("user", "已建立项目", plan.acquire_time, None, plan.source_refs),),
                        state=ObjectState(state), recall_tier=RecallTier(tier), linked_topic_id=topic)
    async with repo._database.transaction() as session:
        session.add(object_mappers.memory_object_to_row(candidate))
    async with repo._database.db_session() as session:
        row = await session.get(MemoryObjectRow, candidate.object_id)
        assert object_mappers.memory_object_to_domain(row) == candidate
        assert row.events_json[0]["occurrence_time"] is None
        assert row.events_json[0]["acquire_time"] == "2026-10-31"
        assert row.plans_json[0]["target_time"] == "2026-11-01"
        assert row.events_json[0]["source_refs"][0]["message_id"] == "u"
        assert row.forgetfulness == 0


@pytest.mark.asyncio
async def test_formal_object_source_position_unique_and_character_fk(production_case):
    from sqlalchemy.exc import IntegrityError
    repo, service, _, _, job_id, _, _ = production_case
    await service.process_job(job_id)
    candidate = (await repo.get_job(job_id)).objects[0]
    async with repo._database.transaction() as session:
        session.add(object_mappers.memory_object_to_row(candidate))
    with pytest.raises(IntegrityError):
        async with repo._database.transaction() as session:
            session.add(object_mappers.memory_object_to_row(replace(candidate, object_id="duplicate-source")))
    with pytest.raises(IntegrityError):
        async with repo._database.transaction() as session:
            session.add(object_mappers.memory_object_to_row(replace(candidate, object_id="wrong-owner",
                source_position_key="different-source", character_id="missing")))


@pytest.mark.asyncio
async def test_object_baseline_preserves_data_and_is_repeatable(production_case):
    from sqlalchemy import text
    from storage.sqlite.models import MemoryObjectRow
    repo, _, _, _, job_id, _, _ = production_case
    before = await repo.get_job(job_id)
    # 重复初始化保留已有候选、聊天原文和任务。
    async with repo._database.transaction() as session:
        await session.execute(text("""INSERT INTO memory_candidates
            (candidate_id, character_id, session_id, object_name, statement, evidence_json,
             forgetfulness, candidate_state, created_at)
            VALUES ('legacy', 'c', 's1', '旧事项', '旧内容', '[]', 1, 'staged', '2026-10-31T00:00:00+00:00')"""))
    await repo._database.initialize()
    await repo._database.initialize()
    assert await repo.get_job(job_id) == before
    async with repo._database.db_session() as session:
        assert (await session.get(MessageRow, "u")).content == "我决定明天开始整理项目"
        assert await session.scalar(text("SELECT statement FROM memory_candidates WHERE candidate_id='legacy'")) == "旧内容"
        assert await session.scalar(select(func.count()).select_from(MemoryObjectRow)) == 0
        assert (await session.scalars(text("SELECT version FROM schema_migrations ORDER BY version"))).all() == [1, 2, 3, 4, 5, 6, 7]
        assert not (await session.execute(text("PRAGMA foreign_key_check"))).all()
