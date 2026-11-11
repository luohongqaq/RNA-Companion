"""context.handoff: cohesive domain types; no external I/O."""
from __future__ import annotations

from module.context_compaction.core.domain.references import InheritedSessionRangeRef
from module.shared.context.types.references import SnapshotRef
from module.shared.context.types.summary import StructuredSummary
from dataclasses import dataclass
from enum import StrEnum


class HandoffMode(StrEnum):
    """交接模式"""
    MODEL = "model"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class HandoffPacket:
    """Session交接材料：新交接摘要，或最近有效摘要加连续原文。

    新摘要生成失败或未完成时可采用fallback，不因总结失败直接中断聊天。
    采用后摘要成为新工作摘要，原文按引用继承；来源有效且预算允许才可切段。
    """
    handoff_id: str  # 交接包 ID
    predecessor_session_id: str
    fallback_snapshot_ref: SnapshotRef | None  # fallback沿用的有效摘要引用；model模式为空
    raw_range: InheritedSessionRangeRef | None  # 继承原文的范围引用，正文在加载阶段读取
    source_hash: str
    mode: HandoffMode  # model或 fallback
    structured_summary: StructuredSummary # 实际采用的摘要正文, 要么是fallback的摘要要么是重新生成的session的摘要

    prepared_through_turn_seq: int  # 准备交接时的边界 a
    summary_covered_through_turn_seq: int  # 实际摘要覆盖点 k
    switch_through_turn_seq: int  # 实际切换点 b，原文必须覆盖k+1～b


@dataclass(frozen=True, slots=True)
class HandoffPacketRef:
    """切段事务采用的已保存交接包引用。"""
    handoff_id: str
    source_hash: str
