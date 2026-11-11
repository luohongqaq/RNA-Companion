"""前台选择和总结共享的运行配置。"""
from __future__ import annotations
from dataclasses import dataclass, field
from module.memory_recall.agent.core.domain.policy import Budget


@dataclass(frozen=True, slots=True)
class MemoryRecallConfig:
    selection_model: str
    summary_model: str
    total_seconds: float = 12.0
    target_seconds: float = 11.0
    selection_limit: float = 10.0
    max_model_calls: int = 6
    max_output_tokens: int = 2048
    budget: Budget = field(default_factory=Budget)
    prefetch_enabled: bool = True

    def __post_init__(self) -> None:
        if not self.selection_model or not self.summary_model:
            raise ValueError("记忆选择和总结模型必须配置")
        if not 0 < self.target_seconds <= self.total_seconds or self.selection_limit <= 0:
            raise ValueError("无效的记忆准备截止时间")
        if self.max_model_calls < 1 or self.max_output_tokens < 1:
            raise ValueError("模型调用次数及输出额度必须为正")
