from __future__ import annotations
from module.memory_management.object.core.processing.config import ObjectModelConfig
from typing import Protocol


class ObjectModelPort(Protocol):
    async def complete(self, prompt: str, payload: dict, config: ObjectModelConfig) -> dict: ...
