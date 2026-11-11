"""daily模型请求配置；不设置日记内容长度目标。"""
from __future__ import annotations
from dataclasses import dataclass
from math import isfinite
from typing import Literal


@dataclass(frozen=True, slots=True)
class DailyModelConfig:
    model_name: str = "glm-5.3"
    timeout_seconds: float = 120.0
    max_output_tokens: int = 16000  # API技术上限，不作为长期记忆的内容长度目标
    max_reextractions: int = 2  # 审核reject后允许重新生成的次数
    disable_thinking: bool = False
    reasoning_effort: Literal["low", "high", "max"] = "low"

    def __post_init__(self) -> None:
        if not self.model_name.strip():
            raise ValueError("daily模型名称不能为空")
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("daily模型超时必须为有限正数")
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise ValueError("模型单次输出额度必须为正整数")
        if type(self.max_reextractions) is not int or self.max_reextractions < 0:
            raise ValueError("审核退回重生成次数必须非负")
        if self.reasoning_effort not in ("low", "high", "max"):
            raise ValueError("daily思考强度必须为low、high或max")

    @classmethod
    def for_review(cls) -> "DailyModelConfig":
        """独立于生成模型的默认审核配置，开启DeepSeek低强度思考。"""
        return cls(model_name="deepseek-chat", disable_thinking=False, reasoning_effort="low")
