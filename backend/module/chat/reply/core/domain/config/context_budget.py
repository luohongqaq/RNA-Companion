"""module.chat.reply.core.domain.config.context_budget: 业务类型与配置，不执行外部I/O。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextPromptBudget:
    """一次聊天模型请求的输入容量预算，所有字段以token计。

    分区预算不要求填满。serialized_input_tokens已包含角色卡、历史、
    召回记忆、当前输入及实际序列化开销，最终须满足：
    serialized_input_tokens + reserved_output_tokens + safety_margin
    <= effective_context_limit。输出预留包含模型要求的推理额度。
    模型独立的输入/输出限制仍由调用层检查。
    """
    effective_context_limit: int = 32_000 #输入、输出/推理和安全余量的总额度
    reserved_output_tokens: int = 4_000 #前台接入时须覆盖本次模型实际输出/推理额度
    character_card_budget: int = 10_000 #角色卡的输入预算
    history_budget: int = 3_000 #session窗口额度：继承摘要及连续尾部按实际token扣减原文空间
    recalled_memory_budget: int = 6_000 #按需召回的长期记忆预算，无需每轮装满
    current_turn_budget: int = 2_000 #当前轮全部用户输入及本轮新增材料的预算
    scene_reference_budget: int = 6_000
    safety_margin: int = 4_000 #为计数误差和序列化开销留余量，不作为常规装填空间；实际开销仍计入prompt

    def __post_init__(self) -> None:
        for name in ("effective_context_limit", "character_card_budget", "history_budget",
                     "recalled_memory_budget", "current_turn_budget", "scene_reference_budget"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须为正整数")
        for name in ("reserved_output_tokens", "safety_margin"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} 必须为非负整数")
        if self.reserved_output_tokens + self.safety_margin >= self.effective_context_limit:
            raise ValueError("输出预留和安全余量不能耗尽总上下文容量")

    @property
    def effective_input_limit(self) -> int:
        """兼容现有 PromptBuilder 的输入检查，同时扣除输出预留。"""
        return self.effective_context_limit - self.reserved_output_tokens
