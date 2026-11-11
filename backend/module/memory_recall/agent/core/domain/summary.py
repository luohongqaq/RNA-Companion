"""供回复生成使用的记忆总结视图。

MemorySummary 从完整召回结果提取状态、总结正文、内容哈希和固定版本引用。
该视图不携带原始记忆卡片或工具轨迹，便于回复提示词使用已完成的召回材料。
"""
from __future__ import annotations
from module.shared.memory.types.references import MemoryRevisionRef
from dataclasses import dataclass
from module.memory_recall.agent.core.domain.result import MemoryContext
from typing import Literal


@dataclass(frozen=True, slots=True)
class MemorySummary:
    """供最终提示词构建使用的视图，不包含记忆卡片或检索轨迹。"""
    status: Literal["ready", "empty", "incomplete"]
    text: str
    content_hash: str
    source_refs: tuple[MemoryRevisionRef, ...]

    @classmethod
    def from_context(cls, context: MemoryContext) -> "MemorySummary":
        return cls(context.status, context.summary_text, context.summary_hash, context.selection.selected_refs)
