"""Agent 选择循环的调用、时间与材料预算策略。

集中定义模型调用次数、默认截止时间、硬保护倍率，以及工具输出和最终材料的 Budget 上限。
策略是领域数据，可被执行状态和调用方共同使用，不依赖选择循环的具体实现。
"""
from __future__ import annotations
from dataclasses import dataclass


MAX_MODEL_CALLS = 6


DEADLINE_SECONDS = 10


HARD_TIMEOUT_MULTIPLIER = 2.0


@dataclass(frozen=True, slots=True)
class Budget:
    max_tool_calls: int = 8
    output_token_limit: int = 12000
    final_doc_limit: int = 10
    final_token_limit: int = 48000
    error_reserve: int = 128
