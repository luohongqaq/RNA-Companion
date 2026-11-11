"""固定候选正文读取工具的输入与结果。

ReadInput 校验本轮候选句柄列表，ReadResult 返回已读句柄、原始正文分区和逐候选错误。
实际句柄归属、正式版本核验与材料登记由检索服务和单次读取会话完成。
"""
from __future__ import annotations
from module.memory_recall.retrieval.core.domain.tools.common import (
    Kind,
    Status,
    TimeContext,
    ToolError,
    TopicTimeContext,
)
from pydantic import BaseModel, Field, field_validator
from typing import Any


class ReadError(ToolError):
    """read 的逐项错误，多一个 card_handle 定位。"""

    card_handle: str


class ReadEntry(BaseModel):
    read_handle: str
    card_handle: str
    kind: Kind
    title: str
    metadata: dict[str, Any] = {}   # 来源元数据缺省为空
    sections: dict[str, Any]        # 完整正式正文分区，原样引用
    time_context: TimeContext | TopicTimeContext | None = None


class ReadResult(BaseModel):
    status: Status
    reads: list[ReadEntry] = []
    errors: list[ReadError] = []


class ReadInput(BaseModel):
    """read_memories 参数。"""

    purpose: str = Field(min_length=1, max_length=200)
    card_handles: list[str] = Field(min_length=1, max_length=5)

    @field_validator("card_handles")
    @classmethod
    def _handles_clean(cls, value: list[str]) -> list[str]:
        value = [handle.strip() for handle in value]
        if any(not handle for handle in value):
            raise ValueError("card_handle 不能为空白")
        if len(set(value)) != len(value):
            raise ValueError("card_handles 不能重复")
        return value
