"""T1 创建 job → T2-A 创建 attempt → job 和 turn 同时指向该 attempt → 输入边界保存进 attempt。"""

from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from module.chat.conversation.core.domain.contracts import SaveMessageInput
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from storage.sqlite.models import (
    CharacterRow,
    ConversationRow,
    GenerationAttemptRow,
    GenerationJobRow,
    TurnRow,
)


@pytest_asyncio.fixture
async def prepared_database(tmp_path):
    """与 T1 相同：每条测试使用独立的真实临时数据库。"""
    database = SQLiteDatabase(tmp_path / "test.sqlite3")

    try:
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


@pytest.fixture
def command():
    return SaveMessageInput(
        request_id="request_001",
        conversation_id="conversation_001",
        character_id="character_test",
        content="你好",
    )


@pytest.mark.asyncio
async def test_claim_creates_attempt_and_fixes_input(
    prepared_database,
    command,
):
    """领取任务，固定输入并保存生成资格。"""
    database, chat_repository, generation_repository = prepared_database

    # 1. 准备：真实执行 T1，得到已提交的生成任务。
    saved = await chat_repository.accept_message(command)

    # 2. 执行：领取该任务。
    ticket = await generation_repository.claim_job(saved.job_id)

    # 3. 检查返回的启动票据。
    assert ticket is not None
    assert ticket.job_id == saved.job_id
    assert ticket.turn_id == saved.turn_id
    assert ticket.input_version == 1
    assert ticket.active_token == 1

    boundary = ticket.input_boundary
    assert boundary.conversation_id == command.conversation_id
    assert boundary.character_id == command.character_id
    assert boundary.current_turn.turn_id == saved.turn_id
    assert boundary.current_turn.input_version == 1
    assert boundary.current_turn.message_ids == (saved.message_id,)

    # 第一轮没有摘要，也没有已完成的历史轮。
    assert boundary.working_snapshot is None
    assert boundary.complete_turns.from_seq == 1
    assert boundary.complete_turns.through_seq == 0

    # 4. 使用新的 Session 检查已经提交的数据。
    async with database.db_session() as session:
        attempt = await session.get(
            GenerationAttemptRow,
            ticket.attempt_id,
        )
        job = await session.get(GenerationJobRow, saved.job_id)
        turn = await session.get(TurnRow, saved.turn_id)

        assert attempt is not None
        assert attempt.job_id == saved.job_id
        assert attempt.turn_id == saved.turn_id
        assert attempt.attempt_state == "retrieving"
        assert attempt.input_version == 1
        assert attempt.active_token == ticket.active_token

        assert job.current_attempt_id == ticket.attempt_id
        assert job.attempt_count == 1

        # 领取后尚未冻结提示词，所以仍在准备阶段。
        assert job.state == "need_prepare"

        assert turn.active_attempt_id == ticket.attempt_id
        assert turn.active_token == ticket.active_token
        assert turn.session_id == boundary.session_id

    # 从数据库读回业务对象，检查固定边界确实持久化了。
    persisted = await generation_repository.get_attempt(ticket.attempt_id)
    assert persisted is not None
    assert persisted.input_boundary == boundary


@pytest.mark.asyncio
async def test_repeated_claim_does_not_create_second_attempt(
    prepared_database,
    command,
):
    """重复领取任务，确保不会创建第二条 attempt。"""
    database, chat_repository, generation_repository = prepared_database

    saved = await chat_repository.accept_message(command)

    first = await generation_repository.claim_job(saved.job_id)
    assert first is not None

    # 同一任务已经有存活 attempt，再领取应拒绝。
    second = await generation_repository.claim_job(saved.job_id)

    assert second is None

    async with database.db_session() as session:
        count = await session.scalar(
            select(func.count()).select_from(GenerationAttemptRow)
        )
        assert count == 1

        job = await session.get(GenerationJobRow, saved.job_id)
        turn = await session.get(TurnRow, saved.turn_id)

        # 重复领取不能增加次数、替换指针或增加资格 token。
        assert job.attempt_count == 1
        assert job.current_attempt_id == first.attempt_id
        assert turn.active_attempt_id == first.attempt_id
        assert turn.active_token == first.active_token


@pytest.mark.asyncio
async def test_old_job_cannot_be_claimed_after_new_input(
    prepared_database,
    command,
):
    """旧任务不能在新输入后被领取"""
    database, chat_repository, generation_repository = prepared_database

    # 第一条消息对应输入版本 1。
    first = await chat_repository.accept_message(command)

    # 第二条消息加入同一轮，对应输入版本 2。
    second = await chat_repository.accept_message(
        replace(
            command,
            request_id="request_002",
            content="今天有点累",
        )
    )

    old_ticket = await generation_repository.claim_job(first.job_id)
    assert old_ticket is None

    # 最新任务能正常领取，并固定两条用户消息。
    new_ticket = await generation_repository.claim_job(second.job_id)

    assert new_ticket is not None
    assert new_ticket.input_version == 2
    assert new_ticket.input_boundary.current_turn.message_ids == (
        first.message_id,
        second.message_id,
    )

    async with database.db_session() as session:
        old_job = await session.get(GenerationJobRow, first.job_id)
        assert old_job.state == "obsolete"

        # 旧任务没有创建 attempt，只有最新任务创建了一条。
        count = await session.scalar(
            select(func.count()).select_from(GenerationAttemptRow)
        )
        assert count == 1


@pytest.mark.asyncio
async def test_archived_conversation_cancels_job(
    prepared_database,
    command,
):
    """归档的对话将取消任务"""
    database, chat_repository, generation_repository = prepared_database

    saved = await chat_repository.accept_message(command)

    # 准备“对话已归档”的状态。
    async with database.transaction() as session:
        conversation = await session.get(
            ConversationRow,
            command.conversation_id,
        )
        conversation.state = "archived"

    ticket = await generation_repository.claim_job(saved.job_id)

    assert ticket is None

    async with database.db_session() as session:
        job = await session.get(GenerationJobRow, saved.job_id)
        turn = await session.get(TurnRow, saved.turn_id)

        assert job.state == "cancelled"
        assert job.current_attempt_id is None
        assert job.attempt_count == 0
        assert turn.active_attempt_id is None
        assert turn.active_token == 0

        count = await session.scalar(
            select(func.count()).select_from(GenerationAttemptRow)
        )
        assert count == 0


@pytest.mark.asyncio
async def test_missing_job_returns_none(prepared_database):
    """未找到的任务将返回 None"""
    database, _, generation_repository = prepared_database

    ticket = await generation_repository.claim_job("missing_job")

    assert ticket is None

    async with database.db_session() as session:
        count = await session.scalar(
            select(func.count()).select_from(GenerationAttemptRow)
        )
        assert count == 0


@pytest.mark.asyncio
async def test_claim_failure_rolls_back_attempt_and_pointers(
    prepared_database,
    command,
):
    """领取失败将回滚 attempt 和指针"""
    database, chat_repository, generation_repository = prepared_database

    saved = await chat_repository.accept_message(command)

    # 记录执行前状态，失败后应该完整恢复。
    async with database.db_session() as session:
        turn = await session.get(TurnRow, saved.turn_id)
        original_versions = (
            turn.input_version,
            turn.row_version,
            turn.active_token,
        )

    # 领取过程要更新 job.current_attempt_id，在这里阻止更新。
    async with database.transaction() as session:
        await session.execute(
            text("""
                CREATE TRIGGER fail_claim_job
                BEFORE UPDATE ON generation_jobs
                WHEN NEW.current_attempt_id IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'simulated claim failure');
                END;
            """)
        )

    with pytest.raises(IntegrityError, match="simulated claim failure"):
        await generation_repository.claim_job(saved.job_id)

    # 独立查询 Session 看到的结果，才是数据库实际提交的结果。
    async with database.db_session() as session:
        count = await session.scalar(
            select(func.count()).select_from(GenerationAttemptRow)
        )
        assert count == 0

        job = await session.get(GenerationJobRow, saved.job_id)
        assert job.state == "need_prepare"
        assert job.current_attempt_id is None
        assert job.attempt_count == 0

        turn = await session.get(TurnRow, saved.turn_id)
        assert turn.active_attempt_id is None
        assert (
            turn.input_version,
            turn.row_version,
            turn.active_token,
        ) == original_versions
