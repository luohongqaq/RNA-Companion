"""context.working: cohesive domain types; no external I/O."""
from __future__ import annotations

from module.shared.context.types.messages import CompletedTurnResult, MessageListItem
from module.shared.context.types.turns import CurrentTurnRef
from module.shared.context.types.references import InheritedContextRef, SnapshotRef
from module.shared.context.types.summary import StructuredSummary
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CurrentTurnInput:
    """当前轮冻结引用与消息读取结果；消息必须为user且与引用ID按序一一对应。"""
    turn_ref: CurrentTurnRef #当前轮引用
    user_messages: tuple[MessageListItem, ...] #复用chat消息结构，保留ID、序号和正文


@dataclass(frozen=True, slots=True)
class SnapshotContext:
    """可直接用于组装的摘要正文及其固定来源，不承载快照发布状态。"""
    ref: SnapshotRef
    structured_summary: StructuredSummary
    summary_text: str


@dataclass(frozen=True, slots=True)
class InheritedContext:
    """已加载的前驱Session上下文，包含摘要和连续尾部原文。"""
    ref: InheritedContextRef
    snapshot: SnapshotContext | None
    complete_turns: tuple[CompletedTurnResult, ...]
    memory_processing_pending: bool = False #长期记忆整理是否还未完成。


@dataclass(frozen=True, slots=True)
class WorkingContext:
    """可直接组装提示词的正文材料；来源引用由各材料携带。"""
    working_snapshot: SnapshotContext | None #摘要正文及引用
    complete_turns: tuple[CompletedTurnResult, ...] #复用chat的连续完整轮读取结果
    current_input: CurrentTurnInput #当前输入
    inherited_context: InheritedContext | None #继承摘要和原文及引用


@dataclass(frozen=True, slots=True)
class ContextUsage:
    """计算对话提示词的实际用量"""
    session_id: str
    history_usage_tokens: int
    serialized_prompt_tokens: int
    tokenizer_version: str
