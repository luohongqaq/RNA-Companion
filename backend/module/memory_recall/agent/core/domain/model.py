"""Agent 模型请求与返回的 SDK 无关契约。

MemoryModelRequest 表达消息、工具、模型及调用选项，MemoryModelResult 保存标准化消息、结束原因和用量。
选择和总结共用 MemoryModelPort，通过适配器完成外部调用，业务层不持有具体 SDK 响应。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MemoryModelRequest:
    model: str
    messages: list[dict[str, Any]]
    timeout_seconds: float
    tools: list[dict[str, Any]] | None = None
    thinking_enabled: bool = True
    reasoning_effort: str = "high"
    max_output_tokens: int = 2048


@dataclass(frozen=True, slots=True)
class MemoryModelResult:
    message: dict[str, Any]
    finish_reason: str
    request_id: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
