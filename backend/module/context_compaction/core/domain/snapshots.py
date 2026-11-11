"""已持久化的上下文快照及其来源契约。"""
from __future__ import annotations
from module.shared.context.types.turns import TurnRange
from module.shared.context.types.references import ContextSourceRef, InheritedContextRef
from module.shared.context.types.summary import StructuredSummary, SummaryVersions
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SnapShotState(StrEnum):
    """快照状态"""
    READY = "ready" # 已通过程序校验并持久化，等待完整轮边界采用
    VALID = "valid"
    INVALIDATED = "invalidated" #来源被编辑或删除，禁止继续使用


@dataclass(frozen=True, slots=True)
class SummarySourceManifest:
    """实际用于生成候选的来源，供校验和恢复。"""
    session_id: str
    source_turns: TurnRange | None
    source_refs: tuple[ContextSourceRef, ...] #当前原文及继承尾部的来源修订，session_id区分归属
    source_hash: str
    inherited_context: InheritedContextRef | None #来源清单只记录引用，不复制正文
    inherited_context_included: bool #明确标记是否已纳入继承材料


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """一个session唯一的交接摘要；READY等待采用，VALID表示已采用。"""
    snapshot_id: str
    session_id: str
    state: SnapShotState
    covered_through_turn_seq: int
    summary: StructuredSummary
    summary_text: str
    token_count: int
    content_hash: str
    versions: SummaryVersions
    source_manifest: SummarySourceManifest
    created_at: datetime

    def __post_init__(self) -> None:
        if type(self.covered_through_turn_seq) is not int or self.covered_through_turn_seq < 0:
            raise ValueError("covered_through_turn_seq must be a non-negative integer")
        if self.source_manifest.session_id != self.session_id:
            raise ValueError("source_manifest must belong to the snapshot session")
