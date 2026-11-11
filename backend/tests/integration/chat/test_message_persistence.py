"""消息经过 T1 后，是否正确、完整地保存到了真实 SQLite 数据库中。"""

from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from module.chat.conversation.core.domain.contracts import SaveMessageInput
from module.shared.common.errors.base import IdempotencyConflictError, InvalidStateError, NotFoundError
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from storage.sqlite.models import (
    CharacterRow,
    ConversationRow,
    SessionRow,
    TurnRow,
    MessageRow,
    GenerationJobRow,
    GenerationAttemptRow,
    RequestReceiptRow,
)


@pytest_asyncio.fixture
async def prepared_database(tmp_path):
    """创建真实的临时数据库，并准备角色数据。"""
    database = SQLiteDatabase(tmp_path / "test.sqlite3")

    try:
        # 使用项目实际的迁移建表
        await database.initialize()

        # 第一条消息需要指向一个已经存在的角色
        async with database.transaction() as session:
            session.add(
                CharacterRow(
                    character_id="character_test",
                    name="测试角色",
                    character_setting="你是一个桌宠。",
                )
            )

        repository = SQLiteChatRepository(database)

        yield database, repository
    finally:
        await database.close()


def make_message():
    return SaveMessageInput(
        request_id="request_001",
        conversation_id="conversation_001",
        character_id="character_test",
        content="你好",
    )


@pytest.mark.asyncio
async def test_first_message_creates_complete_records(prepared_database):
    # 用户的第一条消息，conversation、session、turn、message、job、receipt 都会创建
    # 1. 准备真实数据库和仓储
    database, repository = prepared_database
    command = make_message()

    # 2. 执行真实的 T1
    result = await repository.accept_message(command)

    # 3. 检查返回的保存回执
    assert result.request_id == command.request_id
    assert result.save_state == "committed"
    assert result.input_version == 1

    # 使用新的查询 session 检查提交后的数据
    async with database.db_session() as session:
        conversation = await session.get(
            ConversationRow,
            command.conversation_id,
        )
        assert conversation is not None
        chat_session = await session.get(
            SessionRow,
            conversation.current_session_id,
        )
        assert chat_session is not None
        assert chat_session.conversation_id == conversation.conversation_id
        assert chat_session.session_state == "open"

        turn = await session.get(TurnRow, result.turn_id)
        assert turn is not None
        assert turn.session_id == chat_session.session_id
        assert turn.turn_state == "current"
        assert turn.input_version == 1

        message = await session.get(MessageRow, result.message_id)
        assert message is not None
        assert message.turn_id == turn.turn_id
        assert message.content == "你好"
        assert message.role == "user"
        assert message.message_seq == 1

        job = await session.get(GenerationJobRow, result.job_id)
        assert job is not None
        assert job.turn_id == turn.turn_id
        assert job.expected_input_version == 1
        assert job.state == "need_prepare"

        receipt = await session.get(
            RequestReceiptRow,
            (command.request_id, "send_message"),
        )
        assert receipt is not None


@pytest.mark.asyncio
async def test_message_after_completed_turn_creates_next_turn(prepared_database):
    # 下一轮消息，turn、message、job 都会创建
    database, repository = prepared_database
    # 1. 准备：发送第一条消息
    first = await repository.accept_message(make_message())

    # 准备“上一轮已经完成”的数据库状态。
    # 这里先只设置当前被测分支所依赖的完成标记。
    # 它不代表执行或验证了完整的 T3 回复采用流程。
    async with database.transaction() as session:
        first_turn = await session.get(TurnRow, first.turn_id)
        first_turn.turn_state = "complete"

    # 2. 执行：上一轮完成后，再发送新消息
    next_command = SaveMessageInput(
        request_id="request_002",
        conversation_id="conversation_001",
        character_id="character_test",
        content="我们聊点别的吧",
    )

    second = await repository.accept_message(next_command)

    # 3. 检查：创建了不同的 turn，输入版本重新开始
    assert second.turn_id != first.turn_id
    assert second.input_version == 1

    async with database.db_session() as session:
        old_turn = await session.get(TurnRow, first.turn_id)
        new_turn = await session.get(TurnRow, second.turn_id)

        # 原轮次保持完成状态
        assert old_turn.turn_state == "complete"
        assert old_turn.input_version == 1

        # 新轮次仍属于同一个 session
        assert new_turn.session_id == old_turn.session_id
        assert new_turn.turn_seq == old_turn.turn_seq + 1
        assert new_turn.turn_state == "current"
        assert new_turn.input_version == 1

        # 新消息写进新轮次，消息顺序从 1 开始
        message = await session.get(
            MessageRow, second.message_id
        )
        assert message.turn_id == new_turn.turn_id
        assert message.message_seq == 1
        assert message.content == "我们聊点别的吧"

        # 新生成任务指向新轮次
        job = await session.get(
            GenerationJobRow, second.job_id
        )
        assert job.turn_id == new_turn.turn_id
        assert job.expected_input_version == 1
        assert job.state == "need_prepare"

        # conversation、session 不增加，turn 增加到两个
        for model in (ConversationRow, SessionRow):
            count = await session.scalar(
                select(func.count()).select_from(model)
            )
            assert count == 1

        turn_count = await session.scalar(
            select(func.count()).select_from(TurnRow)
        )
        assert turn_count == 2


@pytest.mark.asyncio
async def test_new_message_joins_current_turn(prepared_database):
    # 一轮发送多条消息
    database, repository = prepared_database
    # 1. 准备：先发送第一条消息，建立当前轮
    first = await repository.accept_message(make_message())

    # 第二条消息使用新的 request_id，同一个 conversation
    second_command = SaveMessageInput(
        request_id="request_002",
        conversation_id="conversation_001",
        character_id="character_test",
        content="今天有点累",
    )

    # 2. 执行：追加第二条消息
    second = await repository.accept_message(second_command)

    # 3. 检查：属于同一轮，但消息和生成任务不同
    assert second.turn_id == first.turn_id
    assert second.message_id != first.message_id
    assert second.job_id != first.job_id
    assert second.input_version == 2

    async with database.db_session() as session:
        turn = await session.get(TurnRow, first.turn_id)
        assert turn.input_version == 2
        assert turn.turn_state == "current"

        messages = (
            await session.scalars(
                select(MessageRow)
                .where(MessageRow.turn_id == first.turn_id)
                .order_by(MessageRow.message_seq)
            )
        ).all()

        assert [message.content for message in messages] == [
            "你好",
            "今天有点累",
        ]
        assert [message.message_seq for message in messages] == [1, 2]

        # 第一条输入对应的旧任务失效
        old_job = await session.get(
            GenerationJobRow, first.job_id
        )
        assert old_job.state == "obsolete"

        # 新任务对应新的输入版本
        new_job = await session.get(
            GenerationJobRow, second.job_id
        )
        assert new_job.state == "need_prepare"
        assert new_job.expected_input_version == 2

        # 没有重复创建 conversation、session 或 turn
        for model in (ConversationRow, SessionRow, TurnRow):
            count = await session.scalar(
                select(func.count()).select_from(model)
            )
            assert count == 1


@pytest.mark.asyncio
async def test_same_request_returns_original_result_without_duplicates(prepared_database):
    # 拒绝重复请求
    database, repository = prepared_database
    command = make_message()

    # 同一请求发送两次
    first = await repository.accept_message(command)
    second = await repository.accept_message(command)

    # 两次返回完全相同的保存结果
    assert second == first

    # 数据不能因为重试而重复创建
    async with database.db_session() as session:
        for model in (
            ConversationRow,
            SessionRow,
            TurnRow,
            MessageRow,
            GenerationJobRow,
            RequestReceiptRow,
        ):
            count = await session.scalar(
                select(func.count()).select_from(model)
            )
            assert count == 1, f"{model.__name__} 出现重复记录"

        # 重试也不能增加输入版本
        turn = await session.get(TurnRow, first.turn_id)
        assert turn.input_version == 1


@pytest.mark.asyncio
async def test_changed_content_with_same_request_id_is_rejected(prepared_database):
    # 同一请求id携带不同内容，应该拒绝，同时保证不能影响已经在工作的轮次
    database, repository = prepared_database
    # 先成功保存原请求，再保留原 request_id，只修改正文。
    command = make_message()
    first = await repository.accept_message(command)
    changed = replace(command, content="这是不同的正文")

    with pytest.raises(IdempotencyConflictError):
        await repository.accept_message(changed)

    # 报错只是第一层检查；还要确认原数据没有被改写。
    async with database.db_session() as session:
        message = await session.get(MessageRow, first.message_id)
        assert message.content == command.content
        turn = await session.get(TurnRow, first.turn_id)
        assert turn.input_version == 1
        job = await session.get(GenerationJobRow, first.job_id)
        assert job.state == "need_prepare"
        for model in (
            ConversationRow, SessionRow, TurnRow, MessageRow,
            GenerationJobRow, RequestReceiptRow,
        ):
            count = await session.scalar(select(func.count()).select_from(model))
            assert count == 1, f"{model.__name__} 被冲突请求改变"

    # 原请求仍能正常重试，说明回执没有被冲突请求破坏。
    assert await repository.accept_message(command) == first


async def fail_receipt_inserts(database):
    """只在临时测试库中设置故障，让 T1 的最后一次记录插入失败。"""
    async with database.transaction() as session:
        await session.execute(text("""
            CREATE TRIGGER fail_receipt_insert
            BEFORE INSERT ON request_receipts
            BEGIN
                SELECT RAISE(ABORT, 'simulated receipt failure');
            END;
        """))


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_entire_first_message(prepared_database):
    # 首条消息失败后整体回滚
    database, repository = prepared_database
    await fail_receipt_inserts(database)

    # 保存回执时，前面的消息、任务等已经执行过 INSERT。
    # ABORT 让该语句失败，仓储的事务上下文应撤销整个 T1。
    with pytest.raises(IntegrityError, match="simulated receipt failure"):
        await repository.accept_message(make_message())

    # 另开查询 session，检查失败事务之外实际能看到的结果。
    async with database.db_session() as session:
        for model in (
            ConversationRow, SessionRow, TurnRow, MessageRow,
            GenerationJobRow, RequestReceiptRow,
        ):
            count = await session.scalar(select(func.count()).select_from(model))
            assert count == 0, f"{model.__name__} 没有完整回滚"

        # 角色属于更早已经提交的事务，应继续保留。
        assert await session.get(CharacterRow, "character_test") is not None


@pytest.mark.asyncio
async def test_append_failure_preserves_original_turn_and_job(prepared_database):
    # 追加失败后保留原始轮次和任务
    database, repository = prepared_database
    command = make_message()
    first = await repository.accept_message(command)

    # 记住追加前的版本，失败后不仅要撤销新记录，也要恢复旧记录。
    async with database.db_session() as session:
        turn = await session.get(TurnRow, first.turn_id)
        original_versions = (turn.input_version, turn.row_version, turn.active_token)

    await fail_receipt_inserts(database)
    second_command = replace(
        command, request_id="request_002", content="今天有点累",
    )

    with pytest.raises(IntegrityError, match="simulated receipt failure"):
        await repository.accept_message(second_command)

    async with database.db_session() as session:
        turn = await session.get(TurnRow, first.turn_id)
        assert (turn.input_version, turn.row_version, turn.active_token) == original_versions
        assert turn.turn_state == "current"

        # 追加过程会把旧 job 改成 obsolete；回滚后应恢复。
        old_job = await session.get(GenerationJobRow, first.job_id)
        assert old_job.state == "need_prepare"
        assert old_job.expected_input_version == 1

        original_message = await session.get(MessageRow, first.message_id)
        assert original_message.content == command.content
        assert await session.get(
            RequestReceiptRow, ("request_002", "send_message"),
        ) is None

        for model in (
            ConversationRow, SessionRow, TurnRow, MessageRow,
            GenerationJobRow, RequestReceiptRow,
        ):
            count = await session.scalar(select(func.count()).select_from(model))
            assert count == 1, f"{model.__name__} 残留追加失败的数据"

    assert await repository.accept_message(command) == first


async def assert_chat_record_counts(session, expected):
    """检查 T1 的六类记录数量；attempt 由具体测试另外检查。"""
    for model in (
        ConversationRow, SessionRow, TurnRow, MessageRow,
        GenerationJobRow, RequestReceiptRow,
    ):
        count = await session.scalar(select(func.count()).select_from(model))
        assert count == expected, f"{model.__name__} 记录数量不符合预期"


@pytest.mark.asyncio
async def test_missing_character_does_not_create_conversation(prepared_database):
    # 不存在的角色不创建conversation
    database, repository = prepared_database
    # 输入结构合法，但指向数据库中不存在的角色。
    command = replace(make_message(), character_id="missing_character")

    with pytest.raises(NotFoundError, match="character does not exist"):
        await repository.accept_message(command)

    async with database.db_session() as session:
        await assert_chat_record_counts(session, 0)
        # 原先准备好的角色不受此次失败影响。
        assert await session.get(CharacterRow, "character_test") is not None


@pytest.mark.asyncio
async def test_character_mismatch_does_not_append_message(prepared_database):
    # 角色不匹配，不追加消息
    database, repository = prepared_database
    command = make_message()
    first = await repository.accept_message(command)
    # 第二个角色也真实存在，确保测的是“不匹配”，而不是“不存在”。
    async with database.transaction() as session:
        session.add(CharacterRow(
            character_id="character_other", name="另一个角色",
            character_setting="另一个角色的设定",
        ))
    async with database.db_session() as session:
        turn = await session.get(TurnRow, first.turn_id)
        original_versions = (turn.input_version, turn.row_version, turn.active_token)

    wrong_character = replace(
        command, request_id="request_002", character_id="character_other",
        content="发给错误角色的消息",
    )
    with pytest.raises(InvalidStateError, match="character does not match"):
        await repository.accept_message(wrong_character)

    async with database.db_session() as session:
        await assert_chat_record_counts(session, 1)
        conversation = await session.get(ConversationRow, command.conversation_id)
        assert conversation.character_id == "character_test"
        turn = await session.get(TurnRow, first.turn_id)
        assert (turn.input_version, turn.row_version, turn.active_token) == original_versions
        message = await session.get(MessageRow, first.message_id)
        assert message.content == command.content
        job = await session.get(GenerationJobRow, first.job_id)
        assert job.state == "need_prepare"


@pytest.mark.asyncio
async def test_archived_conversation_rejects_new_message(prepared_database):
    # 归档会话拒绝新消息
    database, repository = prepared_database
    command = make_message()
    first = await repository.accept_message(command)
    # 在准备阶段设置归档状态；本测试不验证归档功能本身。
    async with database.transaction() as session:
        conversation = await session.get(ConversationRow, command.conversation_id)
        conversation.state = "archived"
    async with database.db_session() as session:
        turn = await session.get(TurnRow, first.turn_id)
        original_versions = (turn.input_version, turn.row_version, turn.active_token)

    # 必须使用新 request_id，否则测到的是原回执重放。
    next_command = replace(command, request_id="request_002", content="归档后的新消息")
    with pytest.raises(InvalidStateError, match="conversation is not active"):
        await repository.accept_message(next_command)

    async with database.db_session() as session:
        await assert_chat_record_counts(session, 1)
        conversation = await session.get(ConversationRow, command.conversation_id)
        assert conversation.state == "archived"
        turn = await session.get(TurnRow, first.turn_id)
        assert (turn.input_version, turn.row_version, turn.active_token) == original_versions
        message = await session.get(MessageRow, first.message_id)
        assert message.content == command.content
        job = await session.get(GenerationJobRow, first.job_id)
        assert job.state == "need_prepare"


@pytest.mark.asyncio
async def test_new_input_supersedes_existing_attempt(prepared_database):
    database, repository = prepared_database
    command = make_message()
    first = await repository.accept_message(command)
    generation = SQLiteGenerationRepository(database)
    # 通过真实 T2-A 创建有资格的 retrieving attempt，不手工拼造数据库记录。
    ticket = await generation.claim_job(first.job_id)
    assert ticket is not None
    assert await generation.check_attempt(ticket.attempt_id) is True

    second = await repository.accept_message(replace(
        command, request_id="request_002", content="再补充一句",
    ))

    assert second.turn_id == first.turn_id
    assert second.input_version == 2
    async with database.db_session() as session:
        attempt = await session.get(GenerationAttemptRow, ticket.attempt_id)
        assert attempt.attempt_state == "superseded"
        old_job = await session.get(GenerationJobRow, first.job_id)
        assert old_job.state == "obsolete"
        new_job = await session.get(GenerationJobRow, second.job_id)
        assert new_job.state == "need_prepare"
        assert new_job.turn_id == first.turn_id
        assert new_job.expected_input_version == 2
        turn = await session.get(TurnRow, first.turn_id)
        assert turn.input_version == 2
        assert turn.active_token > ticket.active_token
        assert turn.turn_state == "current"

    # 不仅检查状态标签，也调用实际资格检查，确认旧 attempt 已不可继续采用。
    assert await generation.check_attempt(ticket.attempt_id) is False


@pytest.mark.asyncio
async def test_append_failure_restores_existing_attempt(prepared_database):
    database, repository = prepared_database
    command = make_message()
    first = await repository.accept_message(command)
    generation = SQLiteGenerationRepository(database)
    ticket = await generation.claim_job(first.job_id)
    assert ticket is not None
    assert await generation.check_attempt(ticket.attempt_id) is True

    async with database.db_session() as session:
        turn = await session.get(TurnRow, first.turn_id)
        original_versions = (turn.input_version, turn.row_version, turn.active_token)
        attempt = await session.get(GenerationAttemptRow, ticket.attempt_id)
        assert attempt.attempt_state == "retrieving"
        original_updated_at = attempt.updated_at

    # T1 会先作废旧 attempt，再插入回执。在最后一步报错，验证前面的修改也撤销。
    await fail_receipt_inserts(database)
    with pytest.raises(IntegrityError, match="simulated receipt failure"):
        await repository.accept_message(replace(
            command, request_id="request_002", content="这条消息保存失败",
        ))

    async with database.db_session() as session:
        await assert_chat_record_counts(session, 1)
        turn = await session.get(TurnRow, first.turn_id)
        assert (turn.input_version, turn.row_version, turn.active_token) == original_versions
        assert turn.active_attempt_id == ticket.attempt_id
        assert turn.turn_state == "current"
        attempt = await session.get(GenerationAttemptRow, ticket.attempt_id)
        assert attempt.attempt_state == "retrieving"
        assert attempt.active_token == ticket.active_token
        assert attempt.updated_at == original_updated_at
        job = await session.get(GenerationJobRow, first.job_id)
        assert job.state == "need_prepare"
        assert job.current_attempt_id == ticket.attempt_id
        message = await session.get(MessageRow, first.message_id)
        assert message.content == command.content
        assert await session.get(
            RequestReceiptRow, ("request_002", "send_message"),
        ) is None
        count = await session.scalar(select(func.count()).select_from(GenerationAttemptRow))
        assert count == 1

    # 失败的新输入不应剥夺原生成的资格；原 attempt 可以继续工作。
    assert await generation.check_attempt(ticket.attempt_id) is True

