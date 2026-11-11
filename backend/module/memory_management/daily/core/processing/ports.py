from __future__ import annotations
from module.memory_management.daily.core.processing.config import DailyModelConfig
from typing import Protocol


class DailyModelPort(Protocol):
    async def complete(self, prompt: str, payload: dict, config: DailyModelConfig) -> dict: ...
