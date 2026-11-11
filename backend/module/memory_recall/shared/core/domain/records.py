"""三个召回功能共用的可见材料与固定正文记录。

VisibleItem 表达模型实际看到的候选，ReadRecord 固定正文、版本和时间信息，RecentRecord 增加本次读取句柄。
这些 TypedDict 只描述静态数据形态；缓存保存正文记录，单次读取会话重新签发自己的句柄。
"""
from __future__ import annotations
from typing import Any, TypedDict


class VisibleItem(TypedDict, total=False):
    """工具返回或L0提供的卡片；仅标题卡片没有正文句柄。"""

    card_handle: str
    read_handle: str | None
    title: str
    kind: str
    sections: dict[str, Any]
    matched_by: list[str]
    time_context: dict[str, Any]


class ReadRecord(TypedDict):
    """read_set中的固定版本公开正文；日期信息见time_context，未知交流时刻保持None。"""

    document_id: str
    kind: str
    title: str
    sections: dict[str, Any]
    revision: int
    content_hash: str
    conversation_time: str | None
    time_context: dict[str, Any]


class RecentRecord(ReadRecord):
    """近期实际可见的正文分区及本轮卡片、正文句柄。"""

    card_handle: str
    read_handle: str
