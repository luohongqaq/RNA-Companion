"""取消事务、T3 竞争、迟到结果及真实 HTTP/SSE 链路。"""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from module.chat.conversation.core.domain.events import GenerationCancelledEvent
from module.shared.common.errors.base import InvalidStateError, NotFoundError
from module.chat.reply.core.domain.models import GenerationFailure, GenerationFailureKind
from module.shared.jobs.config.retry import RetryPolicy
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from storage.sqlite.models import ChatEventRow, GenerationAttemptRow, MessageRow, TurnRow
from test_reply_adoption_and_handoff import prepared_database, prepare_candidate, make_message
from test_chat_page_recovery import next_event
from test_sse_end_to_end import database, live_chat, send_message, wait_job
from test_generation_restart import probe


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["queued", "retrieving", "candidate"])
async def test_cancel_preserves_input_and_rejects_late_updates(prepared_database, phase):
    db, chat, repository = prepared_database
    ticket = candidate = None
    if phase == "candidate":
        saved, ticket, candidate = await prepare_candidate(chat, repository)
    else:
        saved = await chat.accept_message(make_message())
        if phase == "retrieving":
            ticket = await repository.claim_job(saved.job_id)
    cancelled = await repository.cancel_job(saved.job_id)
    assert cancelled.status.value == "cancelled" and cancelled.finished_at is not None
    assert cancelled.retry_count == cancelled.manual_retry_count == 0
    assert await repository.cancel_job(saved.job_id) == cancelled
    assert await repository.list_ready_jobs() == ()
    assert await repository.claim_job(saved.job_id) is None
    if ticket is not None:
        assert not await repository.check_attempt(ticket.attempt_id)
        assert not await repository.record_failure(saved.job_id, ticket.attempt_id,
            GenerationFailure(GenerationFailureKind.NETWORK, "timeout", "超时", True,
                              datetime.now(timezone.utc)), RetryPolicy())
    if candidate is not None:
        assert not await repository.save_candidate(ticket, candidate)
        assert await repository.accept_reply(ticket, turn_token_count=20, tokenizer_version="test") is None
    event, = await chat.list_events_after(0)
    assert isinstance(event, GenerationCancelledEvent)
    assert event.job_id == saved.job_id and event.input_version == saved.input_version
    assert event.conversation_id == "conversation_001" and event.manual_retry_count == 0
    async with db.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MessageRow)) == 1
        turn = await session.get(TurnRow, saved.turn_id)
        assert turn.turn_state == "current" and turn.accepted_message_id is None
        if ticket is not None:
            attempt = await session.get(GenerationAttemptRow, ticket.attempt_id)
            assert attempt.attempt_state == "superseded" and attempt.completed_at is not None
    with pytest.raises(InvalidStateError):
        await repository.retry_failed_job(saved.job_id)


@pytest.mark.asyncio
async def test_completed_reply_wins_and_duplicate_cancel_is_safe_for_new_input(prepared_database):
    db, chat, repository = prepared_database
    saved, ticket, _ = await prepare_candidate(chat, repository)
    assert await repository.accept_reply(ticket, turn_token_count=20, tokenizer_version="test")
    with pytest.raises(InvalidStateError):
        await repository.cancel_job(saved.job_id)
    assert [e.event_type for e in await chat.list_events_after(0)] == ["reply_committed"]
    next_saved = await chat.accept_message(replace(make_message(), request_id="next_cancel_input"))
    await repository.cancel_job(next_saved.job_id)
    newest = await chat.accept_message(replace(make_message(), request_id="next_after_cancel"))
    new_ticket = await repository.claim_job(newest.job_id)
    assert new_ticket is not None
    # 已取消旧 job 的重复请求不能修改新 attempt 的 token 或资格。
    await repository.cancel_job(next_saved.job_id)
    assert await repository.check_attempt(new_ticket.attempt_id)
    assert [e.event_type for e in await chat.list_events_after(0)] == ["reply_committed", "generation_cancelled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [True, False])
async def test_concurrent_cancel_and_accept_have_one_winner(prepared_database, cancel_first):
    db, chat, repository = prepared_database
    saved, ticket, _ = await prepare_candidate(chat, repository)
    other = SQLiteGenerationRepository(db)  # 不同仓储对象、不同事务，依靠 SQLite 写锁裁决。
    cancel = other.cancel_job(saved.job_id)
    accept = repository.accept_reply(ticket, turn_token_count=20, tokenizer_version="test")
    operations = [cancel, accept] if cancel_first else [accept, cancel]
    results = await asyncio.gather(*operations, return_exceptions=True)
    cancelled, accepted = results if cancel_first else reversed(results)
    state = (await repository.get_job(saved.job_id)).status.value
    events = await chat.list_events_after(0)
    assert len(events) == 1
    if state == "cancelled":
        assert not isinstance(cancelled, BaseException) and accepted is None
        assert events[0].event_type == "generation_cancelled"
    else:
        assert state == "done" and accepted is not None and not isinstance(accepted, BaseException)
        assert isinstance(cancelled, InvalidStateError)
        assert events[0].event_type == "reply_committed"
    async with db.db_session() as session:
        assert await session.scalar(select(func.count()).select_from(MessageRow)) == (1 if state == "cancelled" else 2)


@pytest.mark.asyncio
async def test_cancel_event_failure_rolls_back_entire_transaction(prepared_database):
    db, chat, repository = prepared_database
    saved, ticket, _ = await prepare_candidate(chat, repository)
    before = await repository.get_job(saved.job_id)
    async with db.transaction() as session:
        await session.execute(text("""CREATE TRIGGER reject_cancel BEFORE INSERT ON chat_events
            WHEN NEW.event_type = 'generation_cancelled'
            BEGIN SELECT RAISE(ABORT, 'cancel event failed'); END"""))
    with pytest.raises(IntegrityError):
        await repository.cancel_job(saved.job_id)
    assert await repository.get_job(saved.job_id) == before
    assert await repository.check_attempt(ticket.attempt_id)
    assert await chat.list_events_after(0) == ()
    assert await repository.accept_reply(ticket, turn_token_count=20, tokenizer_version="test")


@pytest.mark.asyncio
async def test_cancel_rejects_missing_failed_and_stale_job(prepared_database):
    db, chat, repository = prepared_database
    with pytest.raises(NotFoundError):
        await repository.cancel_job("missing")
    saved = await chat.accept_message(make_message())
    current = await chat.accept_message(replace(make_message(), request_id="newer_input"))
    ticket = await repository.claim_job(current.job_id)
    with pytest.raises(InvalidStateError):
        await repository.cancel_job(saved.job_id)
    assert await repository.check_attempt(ticket.attempt_id)
    await repository.record_failure(current.job_id, ticket.attempt_id,
        GenerationFailure(GenerationFailureKind.MODEL, "invalid", "失败", False,
                          datetime.now(timezone.utc)), RetryPolicy())
    with pytest.raises(InvalidStateError):
        await repository.cancel_job(current.job_id)
    assert [e.event_type for e in await chat.list_events_after(0)] == ["generation_failed"]


@pytest.mark.asyncio
async def test_http_cancel_running_model_replay_and_continue_sending(live_chat):
    test = live_chat
    client = test.client
    assert (await client.post("/api/v1/generation-jobs/missing/cancel")).status_code == 404
    saved, path, _ = await send_message(client, 1)
    assert await asyncio.wait_for(test.model.started.get(), 5) == 1
    cancel_path = f"/api/v1/generation-jobs/{saved['job_id']}/cancel"
    responses = await asyncio.gather(client.post(cancel_path), client.post(cancel_path))
    assert all(r.status_code == 200 for r in responses)
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()["status"] == "cancelled"
    assert (await client.get(path.removesuffix("/messages") + "/generation-jobs")).json()["items"] == []
    assert len((await client.get(path)).json()["items"]) == 1
    # 模型仍挂起，但取消已落库；新连接补发取消，说明取消不依赖原 SSE 连接。
    async with client.stream("GET", "/api/v1/events") as stream:
        lines = stream.aiter_lines()
        name, event_id, event = await next_event(lines)
        assert name == "generation_cancelled" and event["job_id"] == saved["job_id"]
        assert event["input_version"] == 1 and event["manual_retry_count"] == 0
        sent = await client.post(path, json={
            "request_id": "after_cancel_request", "character_id": "character_test",
            "content": "上次我为什么说想减少每天的学习任务？",
        })
        assert sent.status_code == 202
        next_saved = sent.json()
        assert next_saved["turn_id"] == saved["turn_id"] and next_saved["input_version"] == 2
        test.model.gates[0].set()  # 旧模型即使返回，也不能成为正式回复。
        assert await asyncio.wait_for(test.model.started.get(), 5) == 2
        test.model.gates[1].set()
        name, next_id, done = await next_event(lines)
        assert name == "reply_committed" and next_id > event_id
        assert done["job_id"] == next_saved["job_id"]
    assert (await wait_job(client, saved["job_id"], "cancelled"))["reply"] is None
    assert (await client.post(cancel_path)).json() == responses[0].json()
    assert (await client.post(f"/api/v1/generation-jobs/{next_saved['job_id']}/cancel")).status_code == 409
    history = (await client.get(path)).json()["items"]
    assert [m["role"] for m in history] == ["user", "user", "character"]
    async with client.stream("GET", f"/api/v1/events?after={event_id - 1}") as stream:
        assert await next_event(stream.aiter_lines()) == ("generation_cancelled", event_id, event)


@pytest.mark.asyncio
async def test_cancelled_job_stays_cancelled_after_process_restart(database, tmp_path):
    path = database.path
    await database.close()
    audit = tmp_path / "cancel-restart.jsonl"
    await probe("interrupt", "cancelled", path, audit)
    await probe("resume", "cancelled", path, audit)
    assert "reply_request" not in audit.read_text(encoding="utf-8")
    reopened = SQLiteDatabase(path)
    try:
        await reopened.initialize()
        chat = SQLiteChatRepository(reopened)
        event, = await chat.list_events_after(0)
        assert isinstance(event, GenerationCancelledEvent)
        job = await SQLiteGenerationRepository(reopened).get_job(event.job_id)
        assert job.status.value == "cancelled"
        async with reopened.db_session() as session:
            assert await session.scalar(select(func.count()).select_from(MessageRow)) == 1
    finally:
        await reopened.close()
