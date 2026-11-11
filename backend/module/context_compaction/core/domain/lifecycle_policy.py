"""session即窗口；继承材料按实际token占用，剩余额度用于本session原文。"""
from dataclasses import dataclass
from math import isfinite

from module.shared.common.errors.base import InvalidStateError
from module.context_compaction.core.domain.lifecycle import LifecycleAction, LifecycleDecision


@dataclass(frozen=True, slots=True)
class SessionLifecycleConfig:
    """按继承材料与本session原文的实际合计用量准备交接。"""
    session_target_tokens: int = 3000 # 窗口额度，不再是可以独立压缩的原文累计进度目标
    session_prepare_ratio: float = 0.80 # 沿用当前80%准备比例
    session_hard_ratio: float = 0.95 #完整轮结束后达到95%进入等待交接，允许单轮越过窗口目标
    handoff_wait_seconds: float = 10.0

    def __post_init__(self) -> None:
        for name in ("session_target_tokens",):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name}必须为正整数")
        for name in ("session_prepare_ratio", "session_hard_ratio", "handoff_wait_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not isfinite(value):
                raise ValueError(f"{name}必须为有限数值")
        if not 0 < self.session_prepare_ratio < self.session_hard_ratio <= 1:
            raise ValueError("窗口比例必须满足0 < 准备比例 < 硬比例 <= 1")
        if self.handoff_wait_seconds <= 0:
            raise ValueError("交接等待时间必须为正数")

    def raw_token_budget(self, *, inherited_tokens: int) -> int:
        """按实际继承用量扣减原文额度；如摘要600，则剩余2400。"""
        self.window_usage_tokens(0, inherited_tokens=inherited_tokens)
        return max(0, self.session_target_tokens - inherited_tokens)

    def window_usage_tokens(self, raw_tokens: int, *, inherited_tokens: int) -> int:
        """窗口用量 = 当前 session 原文 raw_token
         + 继承摘要 token
         + 继承原文尾部 token"""
        if type(raw_tokens) is not int or raw_tokens < 0:
            raise ValueError("raw_tokens必须为非负整数")
        if type(inherited_tokens) is not int or inherited_tokens < 0:
            raise ValueError("inherited_tokens必须为非负整数")
        return raw_tokens + inherited_tokens

    def decide_context_maintenance(
        self,
        *,
        session_id: str,
        through_turn_seq: int,
        session_progress_tokens: int,
        inherited_tokens: int,
        handoff_ready: bool,
        at_complete_turn_boundary: bool,
    ) -> LifecycleDecision:
        """完整轮后80%准备、95%交接；提前READY不触发切换。

        handoff_ready必须已核对来源、切换点之前的全部连续尾部和下一窗口容量；
        仅有旧冻结点的READY摘要并不代表就绪。这里不执行调度或切换事务。
        """
        if not session_id or type(through_turn_seq) is not int or through_turn_seq < 0:
            raise InvalidStateError("session或完整轮边界不合法")
        if type(handoff_ready) is not bool or type(at_complete_turn_boundary) is not bool:
            raise InvalidStateError("交接状态必须为布尔值")
        usage = self.window_usage_tokens(
            session_progress_tokens, inherited_tokens=inherited_tokens,
        )
        if (usage >= self.session_target_tokens * self.session_hard_ratio
                and handoff_ready and at_complete_turn_boundary):
            actions = (LifecycleAction.ROTATE_WHEN_READY,)
        elif usage >= self.session_target_tokens * self.session_hard_ratio:
            actions = (LifecycleAction.PREPARE_HANDOFF, LifecycleAction.WAIT_FOR_CONTEXT)
        elif usage >= self.session_target_tokens * self.session_prepare_ratio:
            actions = (LifecycleAction.PREPARE_HANDOFF,)
        else:
            actions = ()
        return LifecycleDecision(actions, session_id, through_turn_seq)

