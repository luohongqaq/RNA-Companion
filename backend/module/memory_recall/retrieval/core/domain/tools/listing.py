"""正式记忆列举工具的输入与覆盖结果。

ListInput 定义 overview 和 fallback 模式、历史线索、时间与关键词筛选，ListResult 返回候选及扫描覆盖信息。
fallback 的历史线索字段在模型中校验，是否满足本轮查找前提由 Agent 的准入规则判断。
"""
from __future__ import annotations
from module.memory_recall.retrieval.core.domain.tools.common import Kind, Status, TimeContext, TimeRange, ToolError, TopicTimeContext, RecallSearchTerms
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any, Literal, Self


class ListCoverage(BaseModel):
    scope_handle: str
    includes_hot_and_cold: bool = False
    filter_target: Literal["title_and_summary"] = "title_and_summary"
    scan_complete: bool
    result_limit_reached: bool = False
    response_budget_reached: bool = False
    temporal: dict[str, Any] = Field(default_factory=dict)


class ListItem(BaseModel):
    card_handle: str
    read_handle: str | None = None  # 摘要超预算降级时 None，只给候选预览
    kind: Kind
    title: str
    time_context: TimeContext | TopicTimeContext
    sections: dict[str, Any]  # 正式摘要分区：daily→五个公开分区；topic→identity+state


class ListResult(BaseModel):
    status: Status
    coverage: ListCoverage
    items: list[ListItem] = []
    errors: list[ToolError] = []


class ListInput(BaseModel):
    """list_memories 参数：默认值与 fallback 的 history_clue 要求在解析时落实。"""

    scope_handle: Literal["scope_all"] = "scope_all"
    mode: Literal["overview", "fallback"]
    purpose: str = Field(min_length=1, max_length=200)
    history_clue: str | None = Field(default=None, max_length=300)
    time_range: TimeRange | None = None
    unknown_event_time: Literal["include", "exclude"] = "include"
    keywords: list[str] = Field(default_factory=list)
    match: Literal["any", "all"] = "any"
    kinds: list[Kind] = Field(default_factory=lambda: ["daily", "topic"])
    limit: int = Field(default=10, ge=1, le=10)

    @field_validator("keywords", mode="before")
    @classmethod
    def _lists_none_to_empty(cls, value: Any) -> Any:
        return value if value else []

    @field_validator("keywords")
    @classmethod
    def _keywords_clean(cls, value: list[str]) -> list[str]:
        return RecallSearchTerms.normalize(value)

    @field_validator("kinds", mode="before")
    @classmethod
    def _kinds_default_full(cls, value: Any) -> Any:
        return value if value else ["daily", "topic"]

    @field_validator("history_clue")
    @classmethod
    def _clue_strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("history_clue 不能为空白")
        return value

    @model_validator(mode="after")
    def _fallback_needs_clue(self) -> Self:
        if self.mode == "fallback" and self.history_clue is None:
            raise ValueError("fallback 必须提供 history_clue，说明此前交流过这段历史的依据")
        return self
