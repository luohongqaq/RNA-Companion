"""压缩范围、轮次统计与摘要验收结论。"""
from __future__ import annotations
from module.shared.context.types.turns import TurnRange
from module.context_compaction.core.domain.references import CompactionMode
from module.shared.context.types.references import InheritedContextRef
from module.chat.reply.core.domain.config.context_budget import ContextPromptBudget
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from module.context_compaction.core.domain.policy import ContextCompactionPolicy


class CompactionOutcome(StrEnum):
    """交接摘要验收后的处理动作。"""
    ACCEPT = "accept"
    RETRY = "retry"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class CompactionAssessment:
    """摘要验收结果，不代表已经发布或切换session。"""
    outcome: CompactionOutcome
    summary_tokens: int
    target_met: bool #完整渲染后的摘要是否严格小于上限且非空
    reason: str


@dataclass(frozen=True, slots=True)
class TurnTokenInfo:
    """一轮消息的token计数"""
    turn_id: str
    session_id: str
    turn_seq: int
    token_count: int
    cumulative_token_count: int #本Session完整轮原文的前缀和；继承摘要预留由生命周期策略另行计算
    tokenizer_version: str


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    """统一交接计划；冻结前缀全部入摘要"""
    session_id: str
    mode: CompactionMode
    source_turns: TurnRange | None #本session参与生成摘要的完整原文范围
    retained_tail: TurnRange | None #历史协议兼容；新计划固定None，冻结后的新增原文另行接续
    prompt_budget: ContextPromptBudget #提供历史容量等输入预算
    compaction_policy: ContextCompactionPolicy #提供交接摘要目标、严格上限和重试次数
    inherited_context: InheritedContextRef | None #计划固定来源引用，执行时再加载正文
    context_only_turns: TurnRange | None #历史协议兼容；新计划固定None
    planned_summary_tokens: int #生成目标；完整渲染后必须严格小于summary_token_limit
