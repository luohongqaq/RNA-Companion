"""Daily 单次处理的固定执行计划。

DailyPlan 从封存会话划定归属轮次、较早辅助轮次及继承引用，并绑定已有日记忆版本。
计划只描述范围和版本；正文由来源材料加载，计划与生成候选一起形成持久化恢复点。
"""
from __future__ import annotations
from module.shared.context.types.turns import TurnRange
from module.shared.common.errors.base import InvalidStateError
from module.shared.context.types.references import InheritedContextRef
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from dataclasses import dataclass
from datetime import date
from module.memory_management.shared.contracts.source import SealedSessionSource


@dataclass(frozen=True, slots=True)
class DailyPlan:
    """daily生成计划；仅保存固定范围和版本引用，执行时再加载正文。"""
    session_id: str
    character_id: str
    memory_date: date  # 新任务由每轮第一条用户消息的后端当地日期确定
    source_turns: TurnRange  # 本session实际提取新增事实的完整轮范围
    context_only_turns: TurnRange | None = None  # 本session较早的仅供理解的跨日上文
    base_daily_ref: MemoryRevisionRef | None = None  # None表示首次生成；否则固定已有daily版本
    inherited_context: InheritedContextRef | None = None  # session入场时的固定继承引用，仅辅助理解

    @classmethod
    def plan_daily(cls, source: SealedSessionSource, *, memory_date: date,
                   source_message_ids: tuple[str, ...] = (),
                   inherited_context: InheritedContextRef | None = None,
                   base_daily_ref: MemoryRevisionRef | None = None) -> DailyPlan:
        """只计算已加载材料的范围，不读取数据库，不单独保存计划。"""
        message_ids = set(source_message_ids or source.group_daily_message_ids().get(memory_date, ()))
        owned = tuple(turn for turn in source.turns if turn.user_messages[0].message_id in message_ids)
        if not owned:
            raise InvalidStateError("daily归属日期没有可提取的完整轮")
        first_seq, last_seq = owned[0].turn_seq, owned[-1].turn_seq
        if tuple(turn.turn_seq for turn in owned) != tuple(range(first_seq, last_seq + 1)):
            raise InvalidStateError("daily归属日期的完整轮不连续，无法构造单一区间计划")
        return cls(
            session_id=source.session_id, character_id=source.character_id, memory_date=memory_date,
            source_turns=TurnRange(source.session_id, first_seq, last_seq),
            context_only_turns=TurnRange(source.session_id, 1, first_seq - 1) if first_seq > 1 else None,
            base_daily_ref=base_daily_ref, inherited_context=inherited_context,
        )

    def __post_init__(self) -> None:
        if not self.session_id or not self.character_id or type(self.memory_date) is not date:
            raise ValueError("daily计划需要session、角色和归属日期")
        if not isinstance(self.source_turns, TurnRange):
            raise ValueError("daily计划必须包含完整轮提取范围")
        for turn_range in (self.source_turns, self.context_only_turns):
            if turn_range is not None and (turn_range.session_id != self.session_id
                    or turn_range.from_seq < 1 or turn_range.through_seq < turn_range.from_seq):
                raise ValueError("daily计划轮次范围不合法或不属于当前session")
        if self.context_only_turns is not None and self.context_only_turns.through_seq >= self.source_turns.from_seq:
            raise ValueError("daily辅助上文必须早于提取范围")
        if self.base_daily_ref is not None and self.base_daily_ref.kind != MemoryKind.DAILY:
            raise ValueError("daily基础版本引用必须指向daily")
        if self.inherited_context is not None and self.inherited_context.predecessor_session_id == self.session_id:
            raise ValueError("daily继承引用不能指向当前session")
