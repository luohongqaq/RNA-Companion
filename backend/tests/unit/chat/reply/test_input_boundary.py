"""T2-A 单元测试：固定本次生成允许读取的输入范围。"""

import pytest

from module.shared.context.types.references import SnapshotRef
from module.chat.reply.core.domain.materials import GenerationInputBoundary


@pytest.fixture
def make_boundary():
    """提供默认参数，每条测试只修改自己关心的内容。"""
    def factory(**changes):
        values = {
            "conversation_id": "conversation_001",
            "character_id": "character_test",
            "session_id": "session_001",
            "turn_id": "turn_005",
            "turn_seq": 5,
            "input_version": 2,
            "message_ids": ["message_001", "message_002"],
        }
        values.update(changes)
        return GenerationInputBoundary.fix(**values)

    return factory


def test_without_snapshot_reads_history_from_first_turn(make_boundary):
    # 准备和执行：当前为第 5 轮，没有工作摘要。
    boundary = make_boundary()

    # 历史应包含第 1～4 轮，不包含当前第 5 轮。
    assert boundary.working_snapshot is None
    assert boundary.complete_turns.session_id == "session_001"
    assert boundary.complete_turns.from_seq == 1
    assert boundary.complete_turns.through_seq == 4

    # 当前轮单独固定，保留输入版本与消息顺序。
    assert boundary.current_turn.turn_id == "turn_005"
    assert boundary.current_turn.input_version == 2
    assert boundary.current_turn.message_ids == (
        "message_001",
        "message_002",
    )


def test_snapshot_excludes_history_already_covered(make_boundary):
    # 准备：摘要已覆盖第 1～3 轮。
    snapshot = SnapshotRef(snapshot_id="snapshot_001", content_hash="snapshot_hash", covered_through_turn_seq=3)

    # 执行：为当前第 5 轮固定边界。
    boundary = make_boundary(working_snapshot=snapshot)

    # 只需要额外读取第 4 轮原文。
    assert boundary.working_snapshot == snapshot
    assert boundary.complete_turns.from_seq == 4
    assert boundary.complete_turns.through_seq == 4

    # 摘要不会改变本轮输入。
    assert boundary.current_turn.turn_id == "turn_005"
    assert boundary.current_turn.message_ids == (
        "message_001",
        "message_002",
    )


def test_first_turn_has_no_completed_history(make_boundary):
    boundary = make_boundary(
        turn_id="turn_001",
        turn_seq=1,
        input_version=1,
        message_ids=["message_001"],
    )

    # 项目用 from_seq > through_seq 表示空历史范围。
    assert boundary.complete_turns.from_seq == 1
    assert boundary.complete_turns.through_seq == 0

    assert boundary.current_turn.turn_id == "turn_001"
    assert boundary.current_turn.input_version == 1
    assert boundary.current_turn.message_ids == ("message_001",)
