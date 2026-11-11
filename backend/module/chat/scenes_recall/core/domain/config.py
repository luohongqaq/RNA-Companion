"""两阶段场景示例中的阈值/去重规则，可按角色调整。"""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class SceneRecallConfig:
    threshold: float = 0.75
    top_k: int = 1
    candidates: int = 10

    def __post_init__(self):
        if not isfinite(self.threshold) or not -1 <= self.threshold <= 1:
            raise ValueError("场景召回阈值必须在[-1, 1]")
        if not 1 <= self.top_k <= self.candidates <= 100:
            raise ValueError("场景数量必须满足1 <= top_k <= candidates <= 100")
