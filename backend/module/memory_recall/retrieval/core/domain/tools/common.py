"""读取工具共用的状态、时间与错误值对象。

统一记忆类别、匹配通道、完整或部分完成状态、时间范围、Daily 与 Topic 时间说明以及工具错误。
词表清理与时间范围校验供各工具模型复用；本文件不执行外部查询或 Agent 准入判断。
"""
from __future__ import annotations
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field, model_validator
from typing import Any, Literal, Self


Kind = Literal["daily", "topic"]


MatchChannel = Literal["grep", "semantic"]


class Status(str, Enum):
    OK = "ok"
    PARTIAL = "partial"


class TimeContext(BaseModel):
    """daily展示记忆日期；来源交流时刻未知时明确返回None。"""

    conversation_time: str | None = None
    memory_date: str | None = None
    record_dates: list[str] = Field(default_factory=list)
    time_precision: Literal["date", "datetime", "unknown"] = "unknown"


class TopicTimeContext(TimeContext):
    """topic展示已发生事件的整日区间及独立计划日期；未知发生日期不从得知日期推断。"""

    event_time_status: Literal["known", "partial", "unknown"]
    event_intervals: list[dict[str, Any]]
    unknown_event_count: int
    plan_dates: list[dict[str, Any]] = Field(default_factory=list)


class ToolError(BaseModel):
    """errors 的通用条目；具体工具需要定位字段时继承（如 ReadError）。"""

    code: str
    message: str


class RecallSearchTerms:
    @staticmethod
    def normalize(value: list[str] | None) -> list[str]:
        """词表归一：None -> 空表；逐项去空白、拒绝空白项与重复项。"""
        if value is None:
            return []
        stripped = [item.strip() for item in value]
        if any(not item for item in stripped):
            raise ValueError("词项不能为空白")
        if len(set(stripped)) != len(stripped):
            raise ValueError("词项不能重复")
        return stripped

class TimeRange(BaseModel):
    """list_memories 的可选时间范围：start 含端点、end 不含，必须晚于 start。"""

    basis: Literal["conversation_time", "event_time"]
    start: datetime  # pydantic 直接解析带时区偏移的 ISO 8601 字符串
    end: datetime

    @model_validator(mode="after")
    def _ordered_and_aware(self) -> Self:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start/end 必须是带时区偏移的 ISO 8601 时间")
        if self.start >= self.end:
            raise ValueError("end 必须晚于 start")
        return self
