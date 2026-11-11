"""Vector search hit tied to a published memory revision."""
from dataclasses import dataclass

from module.shared.memory.types.references import MemoryRevisionRef


@dataclass(frozen=True, slots=True)
class RecallHit:
    ref: MemoryRevisionRef
    score: float
