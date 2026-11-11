"""用户记忆查询的类别与分页位置契约。

DocumentKind 将该功能限定为 Daily 和 Topic；各自游标按归属日期或更新时间与文档 ID 组合定位。
游标编码、角色归属和翻页方向检查由查询处理层完成，不与 Agent 的搜索条件混用。
"""
from __future__ import annotations
from module.shared.memory.types.references import MemoryKind
from datetime import date, datetime
from typing import Literal


type DocumentKind = Literal[MemoryKind.DAILY, MemoryKind.TOPIC]


type DailyMemoryCursor = tuple[date, str]


type TopicMemoryCursor = tuple[datetime, str]
