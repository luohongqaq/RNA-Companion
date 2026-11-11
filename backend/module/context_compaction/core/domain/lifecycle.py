"""会话生命周期决策，由压缩和回复生成共同使用。"""
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum


class LifecycleAction(StrEnum):
    """完整轮提交后的Session生命周期决策。"""
    NONE = "none"
    # 旧任务兼容值；新的窗口机制不再产生窗口内压缩动作。
    SCHEDULE_SNAPSHOT = "schedule_snapshot"
    # 窗口容量达到准备比例：为下一Session生成交接摘要。
    PREPARE_HANDOFF = "prepare_handoff"
    # 交接材料完整就绪且处于完整轮边界：立即切换。
    ROTATE_WHEN_READY = "rotate_when_ready"
    WAIT_FOR_CONTEXT = "wait_for_context" #窗口到顶仍不能交接，阻止下一轮生成并等待
    PAUSE_GENERATION = "pause_generation" #完整prompt仍超限，保存输入但不调用模型


@dataclass(frozen=True, slots=True)
class LifecycleDecision:
    """窗口交接动作；无动作使用空tuple。"""
    actions: tuple[LifecycleAction, ...]
    session_id: str
    through_turn_seq: int #完整轮来源边界
