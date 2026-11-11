"""上下文加载集成测试：真实 SQLite、真实仓储和加载器。"""

import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from module.chat.conversation.core.domain.contracts import SaveMessageInput
from module.shared.context.types.turns import TurnRange
from module.shared.common.errors.base import InvalidStateError, NotFoundError
from module.chat.reply.core.processing.context_loader import ContextLoader
from module.shared.context.types.references import InheritedContextRef
from module.context_compaction.repository import mappers as context_mappers
from storage.sqlite.value_codecs import SQLiteValueCodec
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from module.chat.reply.repository.context_sqlite import (
    SQLiteContextReadRepository,
)
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.reply.repository.sqlite import (
    SQLiteGenerationRepository,
)
from storage.sqlite.models import (
    CharacterRow,
    ContextSnapshotRow,
    ConversationRow,
    MessageRow,
    SessionRow,
    TurnRow,
)


@pytest_asyncio.fixture
async def prepared_database(tmp_path):
    database = SQLiteDatabase(tmp_path / "context.sqlite3")

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
        context_repository = SQLiteContextReadRepository(database)
        loader = ContextLoader(context_repository)

        yield (
            database,
            chat_repository,
            generation_repository,
            loader,
        )
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


def make_load_arguments(boundary):
    """参数名与当前 ContextLoader.load() 保持一致。"""
    return {
        "working_snapshot_ref": boundary.working_snapshot,
        "complete_turns": boundary.complete_turns,
        "current_turn_ref": boundary.current_turn,
        "inherited_context_ref": boundary.inherited_context,
    }


async def prepare_completed_turn(
    database,
    saved,
    reply_content,
    *,
    add_unaccepted_reply=False,
):
    """准备一个历史完整轮，不执行或验证 T3。

    本组测试关注“读取历史”，因此直接设置已完成轮的数据。
    """
    reply_id = f"reply_{saved.turn_id}"
    now = datetime.now(timezone.utc)

    async with database.transaction() as session:
        turn = await session.get(TurnRow, saved.turn_id)

        last_seq = await session.scalar(
            select(func.max(MessageRow.message_seq)).where(
                MessageRow.turn_id == saved.turn_id
            )
        )

        session.add(
            MessageRow(
                message_id=reply_id,
                turn_id=saved.turn_id,
                message_seq=last_seq + 1,
                role="character",
                content=reply_content,
                visual_json=None,
                created_at=now,
            )
        )

        if add_unaccepted_reply:
            # 故意放入另一条角色消息，检查读取器是否只取正式指针。
            # 这属于读取器的边界数据，不表示正常 T3 会保存多条回复。
            session.add(
                MessageRow(
                    message_id=f"unaccepted_{saved.turn_id}",
                    turn_id=saved.turn_id,
                    message_seq=last_seq + 2,
                    role="character",
                    content="这条回复没有被正式采用",
                    visual_json=None,
                    created_at=now,
                )
            )

        # 先插入消息，再设置外键指针。
        await session.flush()

        turn.accepted_message_id = reply_id
        turn.turn_state = "complete"

        session_id = turn.session_id

    return session_id, reply_id


async def prepare_snapshot(
    database,
    *,
    session_id,
    snapshot_id,
    covered_through,
    summary_text,
):
    """写入符合当前映射格式的摘要。"""
    content_hash = hashlib.sha256(
        summary_text.encode("utf-8")
    ).hexdigest()

    async with database.transaction() as session:
        session.add(
            ContextSnapshotRow(
                snapshot_id=snapshot_id,
                session_id=session_id,
                covered_through_turn_seq=covered_through,
                summary_json={
                    "summary": summary_text,
                    "commitments": [],
                    "active_thread": None,
                    "object_bindings": [],
                    "tone_context": [],
                    "unresolved_questions": [],
                    "latest_topic": None,
                    "source_refs": [],
                    "scene_state": None,
                    "keywords": ["日常对话"],
                },
                rendered_summary_text=summary_text,
                content_hash=content_hash,
                snapshot_state="valid",
                created_at=datetime.now(timezone.utc),
            )
        )


async def prepare_successor_session(
    database,
    *,
    conversation_id,
    predecessor_session_id,
):
    """准备 session 切换后的路由，不测试完整切换业务。"""
    successor_id = "session_successor"
    now = datetime.now(timezone.utc)

    async with database.transaction() as session:
        predecessor = await session.get(
            SessionRow,
            predecessor_session_id,
        )
        predecessor.session_state = "sealed"
        predecessor.session_end_time = now

        # 先封存旧 session，避免违反“只能有一个 open session”的约束。
        await session.flush()

        snapshot = await session.scalar(select(ContextSnapshotRow).where(
            ContextSnapshotRow.session_id == predecessor_session_id,
            ContextSnapshotRow.snapshot_state == "valid",
        ))
        last_seq = await session.scalar(select(func.max(TurnRow.turn_seq)).where(
            TurnRow.session_id == predecessor_session_id,
        )) or 0
        inherited = InheritedContextRef.handoff(predecessor_session_id, last_seq,
            context_mappers.snapshot_to_ref(snapshot) if snapshot is not None else None)
        session.add(
            SessionRow(
                session_id=successor_id,
                session_seq=(await session.get(SessionRow, predecessor_session_id)).session_seq + 1,
                conversation_id=conversation_id,
                predecessor_session_id=predecessor_session_id,
                auto_successor_of=predecessor_session_id,
                inherited_context_json=SQLiteValueCodec.inherited_context_to_json(inherited),
                session_state="open",
                session_progress_tokens=0,
                session_start_time=now,
            )
        )
        await session.flush()

        conversation = await session.get(
            ConversationRow,
            conversation_id,
        )
        conversation.current_session_id = successor_id

    return successor_id

@pytest.mark.asyncio
async def test_loads_current_messages_from_real_database(
    prepared_database,
    command,
):
    """读取当前输入消息"""
    _, chat_repository, generation_repository, loader = prepared_database

    first = await chat_repository.accept_message(command)
    second = await chat_repository.accept_message(
        replace(
            command,
            request_id="request_002",
            content="今天有点累",
        )
    )

    ticket = await generation_repository.claim_job(second.job_id)
    assert ticket is not None

    context = await loader.load(
        **make_load_arguments(ticket.input_boundary)
    )

    messages = context.current_input.user_messages

    assert [item.message_id for item in messages] == [
        first.message_id,
        second.message_id,
    ]
    assert [item.content for item in messages] == [
        "你好",
        "今天有点累",
    ]
    assert [item.message_seq for item in messages] == [1, 2]
    assert all(item.turn_id == second.turn_id for item in messages)
    assert all(item.role == "user" for item in messages)

    assert context.current_input.turn_ref.input_version == 2
    assert context.working_snapshot is None
    assert context.complete_turns == ()
    assert context.inherited_context is None


@pytest.mark.asyncio
async def test_loading_does_not_expand_fixed_input(
    prepared_database,
    command,
):
    """读取当前输入消息，新信息在ticket生成以后进入，不会更新ticket"""
    _, chat_repository, generation_repository, loader = prepared_database

    first = await chat_repository.accept_message(command)
    ticket = await generation_repository.claim_job(first.job_id)
    assert ticket is not None

    # T2-A 固定边界之后，数据库又保存了一条输入。
    second = await chat_repository.accept_message(
        replace(
            command,
            request_id="request_002",
            content="我再补充一句",
        )
    )

    context = await loader.load(
        **make_load_arguments(ticket.input_boundary)
    )

    messages = context.current_input.user_messages

    # 加载器只读取原票据指定的消息，不自动扩展到新输入。
    assert [item.message_id for item in messages] == [
        first.message_id,
    ]
    assert [item.content for item in messages] == ["你好"]
    assert second.message_id not in [
        item.message_id for item in messages
    ]
    assert context.current_input.turn_ref.input_version == 1


@pytest.mark.asyncio
async def test_missing_current_message_fails(
    prepared_database,
    command,
):
    """读取当前输入消息，丢失消息会报错"""
    _, chat_repository, generation_repository, loader = prepared_database

    saved = await chat_repository.accept_message(command)
    ticket = await generation_repository.claim_job(saved.job_id)
    assert ticket is not None

    arguments = make_load_arguments(ticket.input_boundary)
    arguments["current_turn_ref"] = replace(
        ticket.input_boundary.current_turn,
        message_ids=(saved.message_id, "missing_message"),
    )

    with pytest.raises(NotFoundError, match="user message not found"):
        await loader.load(**arguments)


@pytest.mark.parametrize(
    ("turn_seq", "error_type"),
    [
        (1, InvalidStateError),  # 存在，但尚未完成。
        (2, NotFoundError),     # 根本不存在。
    ],
)
@pytest.mark.asyncio
async def test_incomplete_or_missing_history_fails(
    prepared_database,
    command,
    turn_seq,
    error_type,
):
    _, chat_repository, generation_repository, loader = prepared_database

    saved = await chat_repository.accept_message(command)
    ticket = await generation_repository.claim_job(saved.job_id)
    assert ticket is not None

    arguments = make_load_arguments(ticket.input_boundary)

    # 人为设置错误范围，验证真实读取器拒绝残缺历史。
    arguments["complete_turns"] = TurnRange(
        session_id=ticket.input_boundary.session_id,
        from_seq=turn_seq,
        through_seq=turn_seq,
    )

    with pytest.raises(error_type):
        await loader.load(**arguments)


@pytest.mark.asyncio
async def test_history_contains_users_and_only_accepted_reply(
    prepared_database,
    command,
):
    """读取当前输入消息，历史包含用户和只有一条正式回复"""
    database, chat_repository, generation_repository, loader = (
        prepared_database
    )

    # 1. 第一轮包含两条用户消息。
    first = await chat_repository.accept_message(command)
    first_extra = await chat_repository.accept_message(
        replace(
            command,
            request_id="request_002",
            content="我想聊一会儿",
        )
    )

    _, accepted_reply_id = await prepare_completed_turn(
        database,
        first_extra,
        "当然可以，我在听。",
        add_unaccepted_reply=True,
    )

    # 2. 第一轮已经完成，下一条输入应进入第二轮。
    current = await chat_repository.accept_message(
        replace(
            command,
            request_id="request_003",
            content="今天工作有点累",
        )
    )
    assert current.turn_id != first.turn_id

    ticket = await generation_repository.claim_job(current.job_id)
    assert ticket is not None

    # 3. 加载历史和当前输入。
    context = await loader.load(
        **make_load_arguments(ticket.input_boundary)
    )

    assert len(context.complete_turns) == 1

    history = context.complete_turns[0]
    assert history.turn_id == first.turn_id
    assert history.turn_seq == 1

    assert [item.message_id for item in history.user_messages] == [
        first.message_id,
        first_extra.message_id,
    ]
    assert [item.content for item in history.user_messages] == [
        "你好",
        "我想聊一会儿",
    ]

    # 正式回复由 accepted_message_id 决定。
    assert history.accepted_reply.message_id == accepted_reply_id
    assert history.accepted_reply.role == "character"
    assert history.accepted_reply.content == "当然可以，我在听。"

    # 当前轮单独保存，不混入历史。
    assert context.current_input.turn_ref.turn_id == current.turn_id
    assert [
        item.content for item in context.current_input.user_messages
    ] == ["今天工作有点累"]

@pytest.mark.asyncio
async def test_loads_the_single_fixed_snapshot(
    prepared_database,
    command,
):
    """即使后台发布了新摘要，当前已经固定了摘要，仍用当前配套的摘要"""
    database, chat_repository, generation_repository, loader = (
        prepared_database
    )

    # 1. 准备第一轮已完成。
    first = await chat_repository.accept_message(command)
    session_id, _ = await prepare_completed_turn(
        database,
        first,
        "你好呀。",
    )

    await prepare_snapshot(
        database,
        session_id=session_id,
        snapshot_id="snapshot_old",
        covered_through=1,
        summary_text="旧摘要：用户打了招呼。",
    )

    # 2. 创建第二轮，T2-A 固定旧摘要。
    current = await chat_repository.accept_message(
        replace(
            command,
            request_id="request_002",
            content="我们继续聊",
        )
    )
    ticket = await generation_repository.claim_job(current.job_id)
    assert ticket is not None

    fixed_ref = ticket.input_boundary.working_snapshot
    assert fixed_ref is not None
    assert fixed_ref.snapshot_id == "snapshot_old"

    # 4. 仍使用 T2-A 原票据加载。
    context = await loader.load(
        **make_load_arguments(ticket.input_boundary)
    )

    snapshot = context.working_snapshot
    assert snapshot is not None
    assert snapshot.ref == fixed_ref
    assert snapshot.ref.snapshot_id == "snapshot_old"
    assert snapshot.summary_text == "旧摘要：用户打了招呼。"
    assert snapshot.structured_summary.summary == (
        "旧摘要：用户打了招呼。"
    )

    # 第一轮已经被摘要覆盖，不重复加载它的原文。
    assert context.complete_turns == ()
    assert [
        item.content for item in context.current_input.user_messages
    ] == ["我们继续聊"]


@pytest.mark.asyncio
async def test_loads_predecessor_snapshot_and_tail_turns(
    prepared_database,
    command,
):
    """测试能否加载上一个 session 的摘要，以及摘要没有覆盖的尾部完整对话轮"""
    database, chat_repository, generation_repository, loader = (
        prepared_database
    )

    # 1. 旧 session 的第一轮。
    first = await chat_repository.accept_message(command)
    old_session_id, _ = await prepare_completed_turn(
        database,
        first,
        "你好，我在这里。",
    )

    # 2. 旧 session 的第二轮，作为摘要后的尾部。
    second = await chat_repository.accept_message(
        replace(
            command,
            request_id="request_002",
            content="我明天要面试",
        )
    )
    _, second_reply_id = await prepare_completed_turn(
        database,
        second,
        "祝你面试顺利。",
    )

    # 旧摘要只覆盖第一轮，第二轮仍需读取原文。
    await prepare_snapshot(
        database,
        session_id=old_session_id,
        snapshot_id="snapshot_predecessor",
        covered_through=1,
        summary_text="上一段对话中，用户与角色打了招呼。",
    )

    # 3. 准备新的 open session，并建立前驱关联。
    new_session_id = await prepare_successor_session(
        database,
        conversation_id=command.conversation_id,
        predecessor_session_id=old_session_id,
    )

    current = await chat_repository.accept_message(
        replace(
            command,
            request_id="request_003",
            content="我还是有点紧张",
        )
    )

    ticket = await generation_repository.claim_job(current.job_id)
    assert ticket is not None
    assert ticket.input_boundary.session_id == new_session_id

    # 4. 加载新 session 的当前输入及继承材料。
    context = await loader.load(
        **make_load_arguments(ticket.input_boundary)
    )

    # 新 session 自己还没有摘要、历史完整轮。
    assert context.working_snapshot is None
    assert context.complete_turns == ()

    inherited = context.inherited_context
    assert inherited is not None
    assert inherited.ref.predecessor_session_id == old_session_id

    # 继承旧摘要。
    assert inherited.snapshot is not None
    assert inherited.snapshot.ref.snapshot_id == (
        "snapshot_predecessor"
    )
    assert inherited.snapshot.summary_text == (
        "上一段对话中，用户与角色打了招呼。"
    )

    # 只继承摘要之后的第二轮，不重复第一轮。
    assert inherited.ref.source_range.from_seq == 2
    assert inherited.ref.source_range.through_seq == 2

    assert len(inherited.complete_turns) == 1
    tail = inherited.complete_turns[0]

    assert tail.session_id == old_session_id
    assert tail.turn_id == second.turn_id
    assert tail.turn_seq == 2
    assert [
        item.content for item in tail.user_messages
    ] == ["我明天要面试"]
    assert tail.accepted_reply.message_id == second_reply_id
    assert tail.accepted_reply.content == "祝你面试顺利。"

    # 当前输入属于新 session，不混入继承历史。
    assert context.current_input.turn_ref.turn_id == current.turn_id
    assert [
        item.content for item in context.current_input.user_messages
    ] == ["我还是有点紧张"]
