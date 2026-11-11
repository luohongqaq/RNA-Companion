"""三条链共用的封存来源与固定来源清单。

SealedSessionSource 保存有序完整轮，支持按固定轮次范围读取和按后端当地日期分组消息。
MemorySourceManifest 记录实际使用的会话、消息、来源哈希及基础版本，区分新增事实范围与辅助材料。
"""
from __future__ import annotations
from module.shared.context.types.messages import CompletedTurnResult
from module.shared.context.types.turns import TurnRange
from module.shared.memory.types.references import MemoryRevisionRef
from dataclasses import dataclass
from datetime import date, tzinfo
from module.shared.common.time import BackendTime


@dataclass(frozen=True, slots=True)
class MemorySourceManifest:
    """本代次固定来源；辅助上下文不进入新增事实覆盖范围。"""
    character_id: str
    source_session_ids: tuple[str, ...]
    source_message_ids: tuple[str, ...]
    source_content_hash: str
    memory_date: date | None = None
    source_daily_refs: tuple[MemoryRevisionRef, ...] = ()
    base_ref: MemoryRevisionRef | None = None


@dataclass(frozen=True, slots=True)
class SealedSessionSource:
    """daily与object共用的封存session原文，不包含继承摘要及前驱原文。"""
    session_id: str
    character_id: str
    turns: tuple[CompletedTurnResult, ...]

    def select_turns(self, scope: TurnRange) -> tuple[CompletedTurnResult, ...]:
        """精确选取连续完整轮；缺失轮不能静默缩小来源范围。"""
        selected = tuple(turn for turn in self.turns if scope.from_seq <= turn.turn_seq <= scope.through_seq)
        if scope.session_id != self.session_id or tuple(turn.turn_seq for turn in selected) != tuple(
                range(scope.from_seq, scope.through_seq + 1)):
            raise ValueError("daily来源范围不属于该session或存在缺失轮次")
        return selected

    def group_daily_message_ids(self, *, zone: tzinfo | None = None) -> dict[date, tuple[str, ...]]:
        """整轮归属第一条用户消息的后端当地日期。"""
        grouped: dict[date, list[str]] = {}
        for turn in self.turns:
            created_at = turn.user_messages[0].created_at
            memory_date = BackendTime.local(created_at, zone=zone).date()
            grouped.setdefault(memory_date, []).extend(
                message.message_id for message in (*turn.user_messages, turn.accepted_reply)
            )
        return {memory_date: tuple(message_ids) for memory_date, message_ids in grouped.items()}
