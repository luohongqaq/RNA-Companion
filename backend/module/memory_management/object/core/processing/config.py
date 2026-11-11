"""Object提取模型配置。"""
from __future__ import annotations
from dataclasses import dataclass
from math import isfinite
from typing import Literal


@dataclass(frozen=True, slots=True)
class ObjectModelConfig:
    model_name: str = "deepseek-chat"
    reasoning_effort: Literal["low", "high", "max"] = "high"
    timeout_seconds: float = 240.0
    max_output_tokens: int = 16000

    def __post_init__(self) -> None:
        if not self.model_name.strip():
            raise ValueError("object模型名称不能为空")
        if self.reasoning_effort not in ("low", "high", "max"):
            raise ValueError("object思考强度不合法")
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("object模型超时必须为有限正数")
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise ValueError("object模型输出额度必须为正整数")
