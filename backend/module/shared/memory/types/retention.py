"""memory_record.retention: cohesive domain types; no external I/O."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RecallTier(StrEnum):
    """召回池的划分：热池是默认检索，冷池默认不检索但有需要时检索"""
    HOT = "hot"
    COLD = "cold"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class MemoryRetention:
    """Daily/Topic池管理：R=0.5**(t/stability_days)，降池不删除证据。"""
    last_mentioned_at: datetime
    stability_days: float = 7.0
    mention_count: int = 1
    never_forget: bool = False  # 表示无限半衰期，避免JSON中的Infinity
    recall_tier: RecallTier = RecallTier.HOT
