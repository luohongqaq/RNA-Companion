"""参与留存检查的正式热池记忆与待检查object。"""

from dataclasses import dataclass
from datetime import date

from module.shared.memory.types.references import MemoryRevisionRef


@dataclass(frozen=True, slots=True)
class HotMemory:
    character_id: str
    ref: MemoryRevisionRef
    last_mentioned_at: date


@dataclass(frozen=True, slots=True)
class StaleObject:
    """待检查的object。mentioned_on取得知日，不是入库时间。"""

    character_id: str
    object_id: str
    mentioned_on: date
