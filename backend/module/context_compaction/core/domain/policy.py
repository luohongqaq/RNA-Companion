"""统一窗口交接摘要的输出额度与重试策略。"""
from dataclasses import dataclass
from math import isfinite
from typing import Literal

from module.context_compaction.core.domain.plan import CompactionAssessment, CompactionOutcome
from module.shared.common.errors.base import InvalidStateError


@dataclass(frozen=True, slots=True)
class ContextModelConfig:
    """摘要生成客户端的请求配置。"""
    model_name: str = "deepseek-chat"
    timeout_seconds: float = 120.0
    max_output_tokens: int = 6000  # 包含思考及结构化来源元数据，不等于最终摘要额度
    disable_thinking: bool = False
    reasoning_effort: Literal["low", "high", "max"] = "low"

    def __post_init__(self) -> None:
        if not self.model_name.strip():
            raise ValueError("上下文模型名称不能为空")
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("模型超时必须为有限正数")
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise ValueError("模型输出额度必须为正整数")
        if self.reasoning_effort not in ("low", "high", "max"):
            raise ValueError("思考强度必须为low、high或max")


@dataclass(frozen=True, slots=True)
class ContextCompactionPolicy:
    """统一交接摘要的输出额度和重试策略。"""
    summary_token_limit: int = 1000  # 不含边界：完整渲染后的摘要必须少于1000，包含约定、黑话等分区
    summary_target_tokens: int = 800 # 生成目标，800～999通过程序校验也可接受
    max_compression_retries: int = 1  # 保留当前配置：首次生成后额外重试一次

    def __post_init__(self) -> None:
        for name in ("summary_token_limit", "summary_target_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name}必须为正整数")
        if self.summary_target_tokens >= self.summary_token_limit:
            raise ValueError("摘要生成目标必须严格小于摘要上限")
        if type(self.max_compression_retries) is not int or self.max_compression_retries < 0:
            raise ValueError("max_compression_retries必须为非负整数")

    def assess(
        self,
        *,
        summary_tokens: int,
        retries_used: int,
        information_preserved: bool,
    ) -> CompactionAssessment:
        """程序检查与完整渲染计数均通过，才允许发布交接摘要。

        token包含约定、黑话等全部摘要分区；严格少于1000，不因重试耗尽放宽。
        尾部连续性与下一窗口实际容量另在采用时核对，验收不直接切换session。
        """
        for name, value in (("summary_tokens", summary_tokens), ("retries_used", retries_used)):
            if type(value) is not int or value < 0:
                raise InvalidStateError(f"{name}必须为非负整数")
        if type(information_preserved) is not bool:
            raise InvalidStateError("information_preserved必须为布尔值")

        within_limit = 0 < summary_tokens < self.summary_token_limit
        if information_preserved and within_limit:
            outcome = CompactionOutcome.ACCEPT
            reason = "必要信息检查通过，摘要满足严格token上限"
        elif retries_used < self.max_compression_retries:
            outcome = CompactionOutcome.RETRY
            reason = "保留必要信息并重新生成摘要"
        else:
            outcome = CompactionOutcome.REJECT
            reason = "必要信息或摘要额度检查不通过，禁止发布"
        return CompactionAssessment(outcome, summary_tokens, within_limit, reason)
