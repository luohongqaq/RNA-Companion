"""common.pagination: cohesive domain types; no external I/O."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Page[T]:
    """游标分页结果"""
    items: tuple[T, ...] #当前页数据
    next_cursor: str | None #下一页游标，无更多数据时为None
