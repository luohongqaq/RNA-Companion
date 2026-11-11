"""留存降池所需的正式文档、待检查object和索引操作。"""
from typing import Protocol

from module.memory_management.retention.core.domain.retention import HotMemory, StaleObject
from module.shared.memory.types.references import MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier


class MemoryRetentionRepository(Protocol):
    async def list_hot_memories(self) -> tuple[HotMemory, ...]: ...
    async def cool_down_batch(self, memories: tuple[HotMemory, ...]) -> None: ...
    async def list_hot_unlinked_objects(self) -> tuple[StaleObject, ...]: ...
    async def cool_down_objects(self, objects: tuple[StaleObject, ...]) -> None: ...


class MemoryPoolIndex(Protocol):
    async def set_memory_pool(
        self, character_id: str, ref: MemoryRevisionRef, tier: RecallTier,
    ) -> bool: ...
    async def set_object_pool(
        self, character_id: str, object_id: str, tier: RecallTier,
    ) -> bool: ...
