"""最终失败事件与任务状态原子提交，支持重试代次和游标补发。"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from module.shared.common.errors.base import InvalidStateError
from module.chat.conversation.core.processing.events import ChatEventService
from module.chat.conversation.core.domain.contracts import SaveMessageInput
from module.chat.conversation.core.domain.events import ChatEvent, GenerationFailedEvent
from module.chat.reply.core.domain.models import GenerationFailure, GenerationFailureKind
from module.shared.jobs.config.retry import RetryPolicy
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from storage.sqlite.models import CharacterRow


@pytest_asyncio.fixture
async def failed_store(tmp_path):
    database = SQLiteDatabase(tmp_path / "failure.sqlite3")
    try:
        await database.initialize()
        async with database.transaction() as session:
            session.add(CharacterRow(character_id="c", name="test", character_setting="test"))
        chat = SQLiteChatRepository(database)
        saved = await chat.accept_message(SaveMessageInput("request", "conv", "c", "hello"))
        yield database, chat, SQLiteGenerationRepository(database), saved
    finally:
        await database.close()


def failure(retryable=True):
    return GenerationFailure(GenerationFailureKind.NETWORK, "timeout", "生成请求超时",
                             retryable, datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_retry_exhaustion_event_replay_and_manual_retry(failed_store):
    database, chat, generation, saved = failed_store
    policy = RetryPolicy(max_retries=1, delays=(timedelta(0),))
    error = failure()
    assert await generation.record_failure(saved.job_id, None, error, policy)
    assert await chat.list_events_after(0) == ()
    pending = await generation.get_job(saved.job_id)
    assert pending.manual_retry_count == 0 and pending.retry_count == 1
    assert await generation.record_failure(saved.job_id, None, error, policy)
    first, = await chat.list_events_after(0)
    assert isinstance(first, (ChatEvent, GenerationFailedEvent))
    assert first.event_type == "generation_failed"
    assert first.job_id == saved.job_id and first.turn_id == saved.turn_id
    assert first.conversation_id == "conv" and first.input_version == 1
    assert first.manual_retry_count == 0 and first.error == error
    assert (await generation.get_job(saved.job_id)).manual_retry_count == first.manual_retry_count
    # 已终止任务的重复登记和失效 attempt 均不产生事件。
    assert not await generation.record_failure(saved.job_id, None, error, policy)
    retried = await generation.retry_failed_job(saved.job_id)
    assert not await generation.record_failure(saved.job_id, "stale", error, policy)
    assert await generation.record_failure(saved.job_id, retried.current_attempt_id,
                                           failure(False), policy)
    second, = await chat.list_events_after(first.event_id)
    assert second.manual_retry_count == 1 and second.event_id > first.event_id
    assert (await generation.get_job(saved.job_id)).manual_retry_count == second.manual_retry_count
    # 旧失败快照不受重试更新影响；订阅断线补发仍按同一游标读取。
    assert (await chat.list_events_after(0))[0] == first
    subscription = ChatEventService(chat).subscribe(first.event_id)
    try:
        assert await anext(subscription) == second
    finally:
        await subscription.aclose()


@pytest.mark.asyncio
async def test_event_insert_failure_rolls_back_job(failed_store):
    database, chat, generation, saved = failed_store
    async with database.transaction() as session:
        await session.execute(text("""CREATE TRIGGER reject_failure BEFORE INSERT ON chat_events
            BEGIN SELECT RAISE(ABORT, 'event failed'); END"""))
    with pytest.raises(IntegrityError):
        await generation.record_failure(saved.job_id, None, failure(False), RetryPolicy())
    job = await generation.get_job(saved.job_id)
    assert job.status.value == "need_prepare" and job.last_error is None
    assert job.retry_count == 0 and await chat.list_events_after(0) == ()


@pytest.mark.asyncio
async def test_corrupt_failure_snapshot_is_not_silently_skipped(failed_store):
    from storage.sqlite.models import ChatEventRow
    database, chat, generation, saved = failed_store
    await generation.record_failure(saved.job_id, None, failure(False), RetryPolicy())
    event, = await chat.list_events_after(0)
    async with database.transaction() as session:
        row = await session.get(ChatEventRow, event.event_id)
        row.failure_json = {**row.failure_json, "manual_retry_count": 10}
    with pytest.raises(InvalidStateError, match="快照损坏"):
        await chat.list_events_after(0)


def test_generation_job_manual_retry_count_mapping_round_trip():
    from module.chat.reply.repository.mappers import generation_job_to_domain, generation_job_to_row
    from module.chat.reply.core.domain.models import GenerationJob

    job = GenerationJob.register_generate_reply("job", "turn", 1, datetime.now(timezone.utc))
    assert generation_job_to_domain(generation_job_to_row(job)).manual_retry_count == 0
    job.manual_retry_count = 3
    job.retry_count = 2
    restored = generation_job_to_domain(generation_job_to_row(job))
    assert restored.manual_retry_count == 3 and restored.retry_count == 2
