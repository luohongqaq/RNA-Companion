"""本次召回的独立审计材料与版本使用记录。

MemoryAudit 从完整结果复制源卡片、版本、资源哈希、耗时和轨迹，供后续保存与排查。
MemorySourceRef 与 AttemptMemoryRef 表达本次选择或实际使用的版本，不表示记忆生成时的事实证据。
"""
from __future__ import annotations
import copy
from module.shared.memory.types.references import MemoryRevisionRef
from dataclasses import dataclass
from datetime import datetime
from module.memory_recall.agent.core.domain.result import MemoryContext
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class MemoryAudit:
    """独立入库的审查证据；不作为回复模型的输入。"""
    attempt_id: str
    status: Literal["ready", "empty", "incomplete"]
    source_refs: tuple[MemoryRevisionRef, ...]
    source_cards: tuple[dict[str, Any], ...]
    summary_hash: str
    summary_status: str
    reason_code: str
    unresolved: tuple[str, ...]
    skill_hash: str
    summary_prompt_hash: str
    character_view_hash: str
    total_seconds: float
    trajectory: tuple[dict[str, Any], ...]

    @classmethod
    def from_context(cls, context: MemoryContext) -> "MemoryAudit":
        return cls(context.attempt_id, context.status, context.selection.selected_refs,
                   copy.deepcopy(context.source_cards), context.summary_hash, context.summary_status,
                   context.reason_code, context.unresolved, context.skill_hash,
                   context.summary_prompt_hash, context.character_view_hash, context.total_seconds,
                   copy.deepcopy(context.trajectory))


@dataclass(frozen=True, slots=True)
class MemorySourceRef:
    """一次筛选最终选中的记忆版本，不表示文档生成证据。"""
    ref_id: str
    turn_id: str
    ref: MemoryRevisionRef
    selected_at: datetime


@dataclass(frozen=True, slots=True)
class AttemptMemoryRef:
    """一次Attempt实际使用的版本；同Attempt同记忆只记录一次。"""
    attempt_id: str
    ref: MemoryRevisionRef
