"""真实进程中断/新进程恢复，验证已提交阶段的持久性和幂等性。"""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from sqlalchemy import func, select, text

from module.chat.conversation.core.processing.events import ChatEventService
from module.chat.conversation.core.domain.events import ReplyCommittedEvent
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from storage.sqlite.models import (
    ChatEventRow, GenerationAttemptRow, GenerationJobRow, MessageRow, RequestReceiptRow,
)
from test_full_chat import ANSWER, database


async def probe(mode, checkpoint, db_path, audit_path):
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).with_name("restart_probe.py")),
        mode, checkpoint, str(db_path), str(audit_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[3],
    )
    try:
        async with asyncio.timeout(45):
            stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    expected = 42 if mode == "interrupt" else 0
    assert process.returncode == expected, (stdout + stderr).decode("utf-8", errors="replace")


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint,status", [
    ("queued", "need_prepare"),
    ("retrieving", "need_prepare"),
    ("prompt", "need_generate"),
    ("model_returned", "need_generate"),
    ("candidate", "need_accept"),
    ("committed", "done"),
])
async def test_new_process_resumes_committed_checkpoint(database, tmp_path, checkpoint, status):
    path = database.path
    await database.close()  # 后续进程不能依靠父进程的连接、事务或服务内存。
    audit_path = tmp_path / "process-audit.jsonl"
    await probe("interrupt", checkpoint, path, audit_path)
    with sqlite3.connect(path) as connection:
        job_id, old_attempt_id, old_attempt_count, old_retry_count, actual = connection.execute(
            "SELECT job_id,current_attempt_id,attempt_count,retry_count,state FROM generation_jobs"
        ).fetchone()
        assert actual == status and old_retry_count == 0
        if checkpoint != "queued":
            assert old_attempt_id is not None and old_attempt_count == 1
        if checkpoint == "committed":
            old_event_id = connection.execute("SELECT event_id FROM chat_events").fetchone()[0]
    before = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert before[-1]["name"] == "crash:" + checkpoint

    await probe("resume", checkpoint, path, audit_path)
    after = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    resumed = after[len(before):]
    expected_replies = 2 if checkpoint == "model_returned" else 1
    assert sum(item["name"] == "reply_request" for item in after) == expected_replies
    assert sum(item["name"] == "reply_response" for item in after) == expected_replies
    if checkpoint in ("prompt", "model_returned", "candidate", "committed"):
        assert not any(item["name"].startswith("memory:") for item in resumed)
    if checkpoint in ("candidate", "committed"):
        assert not any(item["name"] == "reply_request" for item in resumed)

    # 再启动一次，已完成任务不会重新调用模型，也不会新增消息/事件。
    await probe("resume", checkpoint, path, audit_path)
    final_audit = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert final_audit[len(after):] == [{"mode": "resume", "name": "finished"}]
    reopened = SQLiteDatabase(path)
    try:
        await reopened.initialize()
        repository = SQLiteGenerationRepository(reopened)
        job = await repository.get_job(job_id)
        assert job.status.value == "done" and job.retry_count == job.manual_retry_count == 0
        assert job.attempt_count == 1
        if old_attempt_id is not None:
            assert job.current_attempt_id == old_attempt_id
        async with reopened.db_session() as session:
            for model, count in ((GenerationJobRow, 1), (GenerationAttemptRow, 1),
                                 (MessageRow, 2), (ChatEventRow, 1), (RequestReceiptRow, 1)):
                assert await session.scalar(select(func.count()).select_from(model)) == count
            assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []
            assert await session.scalar(text("PRAGMA integrity_check")) == "ok"
        chat = SQLiteChatRepository(reopened)
        reply = await chat.read_accepted_reply(job_id)
        assert reply.content == ANSWER
        subscription = ChatEventService(chat).subscribe(0)
        try:
            event = await asyncio.wait_for(anext(subscription), 5)
        finally:
            await subscription.aclose()
        assert isinstance(event, ReplyCommittedEvent)
        assert event.job_id == job_id and event.reply == reply
        if checkpoint == "committed":
            assert event.event_id == old_event_id
        assert await chat.list_events_after(event.event_id) == ()
    finally:
        await reopened.close()
