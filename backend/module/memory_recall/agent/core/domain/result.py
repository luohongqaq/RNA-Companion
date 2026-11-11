"""Agent 完整召回结果与已核验的选择材料。

MemoryExcerpt 和 MemorySelection 固定被采用的记忆版本与正文分区，MemoryContext 组合总结、来源卡片与执行轨迹。
完整结果可投影为回复总结或独立审计材料；只有完整交付的总结默认进入回复提示词。
"""
from __future__ import annotations
from module.shared.memory.types.references import MemoryRevisionRef
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class MemoryContext:
    """记忆召回结果：包含总结、源材料、轨迹等信息"""
    attempt_id: str
    status: Literal["ready", "empty", "incomplete"]
    selection: MemorySelection
    source_cards: tuple[dict[str, Any], ...]
    summary_text: str  #最后交付的记忆总结
    summary_hash: str
    summary_status: str #总结状态
    reason_code: str
    unresolved: tuple[str, ...]
    skill_hash: str
    summary_prompt_hash: str
    character_view_hash: str
    total_seconds: float #总耗时
    trajectory: tuple[dict[str, Any], ...] #本次记忆agent循环的轨迹

    @property
    def prompt_text(self) -> str:
        """仅完整交付的总结可以默认注入回复提示词；未完成结果由调用方处理。"""
        return self.summary_text if self.status == "ready" else ""


@dataclass(frozen=True, slots=True)
class MemoryExcerpt:
    """记忆稳定的section及正文text"""
    ref: MemoryRevisionRef
    section_key: str
    text: str


@dataclass(frozen=True, slots=True)
class MemorySelection:
    """只引用已校验材料；已固定历史版只用于本Attempt，不重入普通召回。"""
    selected_refs: tuple[MemoryRevisionRef, ...] = () #版本号
    excerpts: tuple[MemoryExcerpt, ...] = () #引用的section和text
    search_again: bool = False
