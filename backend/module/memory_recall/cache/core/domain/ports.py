"""跨轮缓存与更新信号的持久化能力接口。

RecallCachePort 约定在同一读取快照中取得缓存、水位及变化，并按缓存版本比较交换保存。
SQLite 实现承担事务和并发核对，业务刷新服务不直接依赖数据库。
"""
from __future__ import annotations
from module.memory_recall.cache.core.domain.cache import RecallCache, RecallCacheView
from typing import Protocol


class RecallCachePort(Protocol):
    async def load(self, character_id: str, conversation_id: str) -> RecallCacheView:
        """同一读快照加载缓存及更新水位；水位未变时不加载来源清单。"""
        ...

    async def save(self, character_id: str, conversation_id: str, cache: RecallCache) -> bool:
        """按cache.version比较并交换，防止迟到attempt覆盖新缓存。"""
        ...
