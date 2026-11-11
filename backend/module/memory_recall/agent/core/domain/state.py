"""单次选择循环的可变执行状态。

AttemptState 统一维护软硬截止时间、模型和工具调用预算、可见句柄、已读材料及执行轨迹。
每次 attempt 独立创建；状态检查可使过期输入取消，材料预算由执行器使用注入的计数器核对。
"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, field
from module.memory_recall.agent.core.domain.policy import (
    Budget,
    DEADLINE_SECONDS,
    HARD_TIMEOUT_MULTIPLIER,
    MAX_MODEL_CALLS,
)
from module.memory_recall.shared.core.domain.records import ReadRecord
from typing import Any, Callable


@dataclass
class AttemptState:
    """一轮记忆选择的预算、可见句柄和执行轨迹。

    功能：集中保存校验、执行和主循环共享的可变状态。
    read_set/cards 仅包含实际提供给本轮模型的材料。
    输入：deadline 使用 time.monotonic() 的截止时刻，0 表示单独调用工具时
    未设置截止；max_model_calls 可覆盖模型调用上限；is_current 可选地检查
    输入是否仍有效；budget 为本轮预算上限，缺省用默认 Budget。
    其他字段默认从零或空集合开始。
    输出：可被执行器原地更新的状态实例，包含计数、已读范围、固定
    版本、finish 拒绝次数及轨迹。
    """

    deadline: float = 0.0  # time.monotonic()；0 表示单独调用工具时未设置截止。
    target_deadline: float = 0.0  # 提醒模型的软目标，不用于取消在途请求。
    model_calls: int = 0
    tool_calls: int = 0
    tool_calls_by_name: dict[str, int] = field(default_factory=dict)
    output_tokens: int = 0
    read_set: dict[str, ReadRecord] = field(default_factory=dict)
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    cards: dict[str, str] = field(default_factory=dict)
    max_model_calls: int = MAX_MODEL_CALLS
    is_current: Callable[[], bool] | None = None
    finish_rejections: int = 0
    budget: Budget = field(default_factory=Budget)

    @classmethod
    def init(cls, deadline_seconds: float, max_model_calls: int,
             is_current: Callable[[], bool] | None,
             budget: "Budget | None" = None) -> "AttemptState":
        """按截止秒数（及可选预算覆盖）建一次完整选择轮的状态。"""
        started = time.monotonic()
        return AttemptState(deadline=started + deadline_seconds * HARD_TIMEOUT_MULTIPLIER,
                            target_deadline=started + deadline_seconds,
                            max_model_calls=max_model_calls, is_current=is_current,
                            budget=budget if budget is not None else Budget())

    def remaining(self) -> float | None:
        """距总截止还剩多少秒；已到期归零，未设截止（deadline 为 0）返回 None。"""
        return max(0.0, self.deadline - time.monotonic()) if self.deadline else None

    def ensure_current(self) -> None:
        """输入新鲜度闸门：is_current 回调返回 False 时抛 CancelledError 作废本轮。"""
        if self.is_current is not None and not self.is_current():
            raise asyncio.CancelledError("当前输入已变化，丢弃旧 attempt")

    def target_remaining(self) -> float | None:
        """模型应争取完成的剩余时间；归零只催促提交，不等于请求超时。"""
        return max(0.0, self.target_deadline - time.monotonic()) if self.target_deadline else self.remaining()

    def must_force_finish(self, index: int) -> bool:
        """是否强制收尾：最后一轮、修正被拒提交、工具名额只剩收尾、返回预算见底。"""
        return (index == self.max_model_calls - 1 or self.finish_rejections > 0
                or (self.target_remaining() is not None and self.target_remaining() <= 3)
                or self.tool_calls >= self.budget.max_tool_calls - 1
                or self.output_tokens >= self.budget.output_token_limit - self.budget.error_reserve)
