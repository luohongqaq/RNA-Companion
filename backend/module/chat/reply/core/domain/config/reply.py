"""回复模型参数；默认值与 backend/.env.example 的示例配置一致。"""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class ReplyModelConfig:
    model_name: str = "glm-5.3-flashx"
    temperature: float = 1.0
    max_output_tokens: int = 500
    timeout_seconds: float = 30.0
    reasoning_effort: str | None = "low"

    def __post_init__(self) -> None:
        if not self.model_name.strip():
            raise ValueError("回复模型名称不能为空")
        if not isfinite(self.temperature) or not 0 <= self.temperature <= 2:
            raise ValueError("无效的temperature")
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise ValueError("回复输出额度必须为正整数")
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("回复超时必须为正")
        if self.reasoning_effort not in (None, "low", "high", "max"):
            raise ValueError("回复思考强度必须为low/high/max或None")
