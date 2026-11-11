"""module.chat.reply.core.domain.config.budget: 业务类型与配置，不执行外部I/O。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptBudget:
    """提示词预算"""
    total_input_budget: int #输入总预算
    reply_reserve: int #回复预留
    section_limits: dict[str, int] #各分区限制
