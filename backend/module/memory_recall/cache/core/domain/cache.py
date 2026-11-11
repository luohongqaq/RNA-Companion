"""跨轮记忆正文缓存及一次一致性读取视图。

RecallCache 保存近期更新、近期检索正文、信号水位和缓存版本，RecallCacheView 组合该快照与新增更新信号。
刷新未完整完成时保留安全材料子集；具体刷新策略和 CAS 保存分别由 service 与仓储实现。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from module.memory_recall.cache.core.domain.update import MemoryUpdate
from module.memory_recall.shared.core.domain.records import ReadRecord


@dataclass(slots=True)
class RecallCache:
    version: int = 0
    signal_seq: int = 0
    recent_update_memory: list[ReadRecord] = field(default_factory=list)
    recent_retrievals: list[ReadRecord] = field(default_factory=list)
    refresh_complete: bool = True #刷新源尚未就绪时只提供安全子集，不覆盖持久缓存或消费水位


@dataclass(frozen=True, slots=True)
class RecallCacheView:
    cache: RecallCache
    latest_signal_seq: int
    changes: tuple[MemoryUpdate, ...] = ()
    recent_updates: tuple[MemoryUpdate, ...] = ()
