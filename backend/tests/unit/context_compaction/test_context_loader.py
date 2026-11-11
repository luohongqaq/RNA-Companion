"""上下文加载单元测试：用 Fake 读取器代替数据库。"""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from module.shared.context.types.messages import MessageListItem
from module.shared.context.types.turns import CurrentTurnRef, TurnRange
from module.shared.context.types.messages import MessageRole
from module.shared.common.errors.base import InvalidStateError, NotFoundError
from module.chat.reply.core.processing.context_loader import ContextLoader
from module.shared.context.types.references import SnapshotRef
from module.shared.context.types.summary import StructuredSummary
from module.shared.context.types.working import SnapshotContext


class FakeContextReadRepository:
    """只记录请求并返回准备好的材料，不执行数据库操作。"""

    def __init__(self):
        self.snapshot_requests = []
        self.turn_requests = []
        self.message_requests = []

        self.snapshot_result = None
        self.turn_result = ()
        self.message_result = ()
        self.message_error = None

    async def read_snapshot(self, ref):
        self.snapshot_requests.append(ref)
        return self.snapshot_result

    async def read_complete_turns(self, session_id, turn_seqs):
        self.turn_requests.append((session_id, turn_seqs))
        return self.turn_result

    async def read_user_messages(self, message_ids):
        self.message_requests.append(message_ids)

        if self.message_error is not None:
            raise self.message_error

        return self.message_result


@pytest.fixture
def repository():
    return FakeContextReadRepository()


@pytest.fixture
def message():
    return MessageListItem(
        message_id="message_001",
        turn_id="turn_001",
        message_seq=1,
        role=MessageRole.USER,
        content="你好",
        visual=None,
        created_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
    )


@pytest.fixture
def load_arguments():
    """第一轮的默认读取范围：没有摘要、历史和继承材料。"""
    return {
        "working_snapshot_ref": None,
        "complete_turns": TurnRange(
            session_id="session_001",
            from_seq=1,
            through_seq=0,
        ),
        "current_turn_ref": CurrentTurnRef(
            turn_id="turn_001",
            input_version=1,
            message_ids=("message_001",),
        ),
        "inherited_context_ref": None,
    }


@pytest.mark.asyncio
async def test_first_turn_loads_only_current_input(
        repository,
    message,
    load_arguments,
):
    """第一轮的默认读取范围：没有摘要、历史和继承材料。"""
    # 1. 准备：读取器提供当前用户消息。
    repository.message_result = (message,)
    loader = ContextLoader(repository)

    # 2. 执行。
    context = await loader.load(**load_arguments)

    # 3. 检查正文和来源引用。
    assert context.current_input.turn_ref == load_arguments["current_turn_ref"]
    assert context.current_input.user_messages == (message,)
    assert context.current_input.user_messages[0].content == "你好"

    assert context.working_snapshot is None
    assert context.complete_turns == ()
    assert context.inherited_context is None

    # 不需要的材料，不应该访问读取器。
    assert repository.snapshot_requests == []
    assert repository.turn_requests == []
    assert repository.message_requests == [("message_001",)]


@pytest.mark.asyncio
async def test_multiple_current_messages_are_preserved(
    repository,
    message,
    load_arguments,
):
    second = replace(
        message,
        message_id="message_002",
        message_seq=2,
        content="今天有点累",
    )
    repository.message_result = (message, second)

    load_arguments["current_turn_ref"] = CurrentTurnRef(
        turn_id="turn_001",
        input_version=2,
        message_ids=("message_001", "message_002"),
    )

    context = await ContextLoader(repository).load(**load_arguments)

    assert repository.message_requests == [
        ("message_001", "message_002"),
    ]
    assert [
        item.content
        for item in context.current_input.user_messages
    ] == ["你好", "今天有点累"]
    assert context.current_input.turn_ref.input_version == 2


@pytest.mark.asyncio
async def test_loads_specified_snapshot_and_history_range(
        repository,
    message,
    load_arguments,
):
    # 摘要覆盖第 1～3 轮，历史原文需要第 4～5 轮。
    snapshot_ref = SnapshotRef(snapshot_id="snapshot_old", content_hash="snapshot_hash", covered_through_turn_seq=3)
    repository.snapshot_result = SnapshotContext(
        ref=snapshot_ref,
        structured_summary=StructuredSummary(
            summary="用户最近在准备面试。",
        ),
        summary_text="用户最近在准备面试。",
    )
    repository.message_result = (
        replace(message, turn_id="turn_006"),
    )

    load_arguments.update(
        working_snapshot_ref=snapshot_ref,
        complete_turns=TurnRange(
            session_id="session_001",
            from_seq=4,
            through_seq=5,
        ),
        current_turn_ref=CurrentTurnRef(
            turn_id="turn_006",
            input_version=1,
            message_ids=("message_001",),
        ),
    )

    context = await ContextLoader(repository).load(**load_arguments)

    assert repository.snapshot_requests == [snapshot_ref]
    assert context.working_snapshot == repository.snapshot_result
    assert context.working_snapshot.summary_text == "用户最近在准备面试。"

    # 加载器负责把连续范围展开为具体轮次。
    assert repository.turn_requests == [
        ("session_001", (4, 5)),
    ]

    # 本测试只检查调用范围；历史是否齐全由真实读取器测试保证。


@pytest.mark.asyncio
async def test_message_from_another_turn_is_rejected(
        repository,
    message,
    load_arguments,
):
    repository.message_result = (
        replace(message, turn_id="another_turn"),
    )

    with pytest.raises(
        InvalidStateError,
        match="user message belongs to another turn",
    ):
        await ContextLoader(repository).load(**load_arguments)


@pytest.mark.parametrize(
    "error_type",
    [NotFoundError, RuntimeError],
)
@pytest.mark.asyncio
async def test_reading_failure_propagates(
        repository,
    load_arguments,
    error_type,
):
    error = error_type("模拟读取失败")
    repository.message_error = error

    with pytest.raises(error_type) as caught:
        await ContextLoader(repository).load(**load_arguments)

    # 不吞异常，也不返回缺少当前输入的上下文。
    assert caught.value is error
