"""context.references: cohesive domain types; no external I/O."""
from __future__ import annotations

from module.shared.context.types.turns import TurnRange
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    """上下文快照引用"""
    snapshot_id: str #快照id
    content_hash: str #内容哈希
    covered_through_turn_seq: int #覆盖到的轮次序号


@dataclass(frozen=True, slots=True)
class InheritedContextRef:
    """继承上下文来源引用；只保存标识、范围和hash，不包含正文。"""
    predecessor_session_id: str #前驱会话id
    summary_snapshot_ref: SnapshotRef | None #前驱session唯一摘要的固定ID及hash；无摘要时为None
    source_range: TurnRange | None #原文范围
    source_hash: str | None #来源hash

    @classmethod
    def handoff(cls, predecessor_session_id: str, last_turn_seq: int,
                snapshot: SnapshotRef | None = None) -> "InheritedContextRef":
        """无自身摘要的session继承上一会话材料：前驱摘要覆盖点之后的全部原文构成尾部范围"""
        covered = snapshot.covered_through_turn_seq if snapshot else 0
        return cls(
            predecessor_session_id=predecessor_session_id,
            summary_snapshot_ref=snapshot,
            source_range=TurnRange(session_id=predecessor_session_id,
                                   from_seq=covered + 1, through_seq=last_turn_seq),
            source_hash=None, #来源hash在T2-B读取实际材料后补齐
        )


@dataclass(frozen=True, slots=True)
class ContextSourceRef:
    """表示某条摘要内容依据哪个来源、来源是哪一版。"""
    turn_id: str
    revision: int
    session_id: str | None = None #原文来源必须标识所属Session
