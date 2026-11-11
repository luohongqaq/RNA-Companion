"""正式记忆搜索工具的输入、覆盖信息与候选结果。

SearchInput 校验 grep、semantic、hybrid 模式的字段组合、关键词、时间和数量范围。
SearchResult 保留分通道完成情况、候选句柄、实际可见分区和逐项错误，供读取与后续选择使用。
"""
from __future__ import annotations
from module.memory_recall.retrieval.core.domain.tools.common import Kind, MatchChannel, Status, TimeContext, TimeRange, ToolError, TopicTimeContext, RecallSearchTerms
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any, Literal, Self


class SearchCoverage(BaseModel):
    scope_handle: str                            # 回显本次范围入口
    search_target: Literal["title_and_summary"] = "title_and_summary"
    literal_scan_complete: bool | None = None    # 未执行 grep 通道时 None
    semantic_query_completed: bool | None = None  # 未执行 semantic 通道时 None
    result_limit_reached: bool = False
    response_budget_reached: bool = False
    temporal: dict[str, Any] = Field(default_factory=dict)


class SearchItem(BaseModel):
    card_handle: str
    read_handle: str | None = None  # 摘要超预算降级时 None，只给卡片
    kind: Kind
    title: str
    matched_by: list[MatchChannel]
    time_context: TimeContext | TopicTimeContext
    sections: dict[str, Any] = {}   # 该类型的正式摘要分区：daily→五个公开分区；topic→identity+state


class SearchResult(BaseModel):
    status: Status
    coverage: SearchCoverage
    items: list[SearchItem] = []
    errors: list[ToolError] = []


class SearchInput(BaseModel):
    """search_memories 参数：字段与 schemas.SEARCH_MEMORIES 一一对应，
    默认值与静态跨字段规则在解析时落实。limit 为每个记忆类型的卡片数
    上限（两类都搜时各最多 limit 张）；每类排序前 5 张附带正式摘要，
    其余仅返回标题。"""

    scope_handle: Literal["scope_hot"] = "scope_hot"
    purpose: str = Field(min_length=1, max_length=200)
    mode: Literal["grep", "semantic", "hybrid"]
    literal_terms: list[str] = Field(default_factory=list)
    match: Literal["any", "all"] = "any"
    query: str | None = Field(default=None, max_length=600)
    kinds: list[Kind] = Field(default_factory=lambda: ["daily", "topic"])
    limit: int = Field(default=10, ge=1, le=10)
    time_range: TimeRange | None = None
    unknown_event_time: Literal["include", "exclude"] = "include"

    @field_validator("literal_terms", mode="before")
    @classmethod
    def _lists_none_to_empty(cls, value: Any) -> Any:
        return value if value else []

    @field_validator("literal_terms")
    @classmethod
    def _terms_clean(cls, value: list[str]) -> list[str]:
        return RecallSearchTerms.normalize(value)

    @field_validator("kinds", mode="before")
    @classmethod
    def _kinds_default_full(cls, value: Any) -> Any:
        return value if value else ["daily", "topic"]

    @field_validator("query")
    @classmethod
    def _query_strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("query 不能为空白")
        return value

    @model_validator(mode="after")
    def _mode_fields(self) -> Self:
        if self.mode in ("grep", "hybrid") and not self.literal_terms:
            raise ValueError("grep/hybrid 必须提供非空 literal_terms")
        if self.mode in ("semantic", "hybrid") and self.query is None:
            raise ValueError("semantic/hybrid 必须提供 query")
        if self.mode == "grep" and self.query is not None:
            raise ValueError("grep 模式不适用 query；需要语义描述请改用 semantic/hybrid")
        if self.mode == "semantic" and self.literal_terms:
            raise ValueError("semantic 模式不适用 literal_terms；有可靠原词请改用 grep/hybrid")
        return self
