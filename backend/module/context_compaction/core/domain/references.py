"""压缩模式与前驱原文来源清单。"""
from dataclasses import dataclass
from enum import StrEnum

from module.shared.context.types.references import ContextSourceRef


@dataclass(frozen=True, slots=True)
class InheritedSessionRangeRef:
    """前驱Session连续原文的来源范围及修订引用，不包含消息正文。"""
    session_id: str
    from_turn_seq: int
    through_turn_seq: int
    source_hash: str
    source_refs: tuple[ContextSourceRef, ...]


class CompactionMode(StrEnum):
    """统一交接摘要生成，不区分增量或rebuild。"""
    HANDOFF = "handoff"
