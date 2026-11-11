"""Topic批处理与新建门槛配置，不限制单篇长期记忆字数。"""
from __future__ import annotations
from module.shared.memory.types.retention import RecallTier
from dataclasses import dataclass
from math import isfinite
from typing import Literal


@dataclass(frozen=True, slots=True)
class TopicProductionConfig:
    max_batch_objects: int = 5  # 仅限制本批新object，不包含召回的历史材料
    min_distinct_sessions: int = 3

    def __post_init__(self) -> None:
        if (type(self.max_batch_objects) is not int or self.max_batch_objects < 1
                or type(self.min_distinct_sessions) is not int or self.min_distinct_sessions < 1):
            raise ValueError("topic批大小和建档session门槛必须为正整数")


@dataclass(frozen=True, slots=True)
class TopicModelConfig:
    model_name: str = "deepseek-chat"
    reasoning_effort: Literal["low", "high", "max"] = "high"
    timeout_seconds: float = 240.0
    max_output_tokens: int = 32000

    def __post_init__(self) -> None:
        if not self.model_name.strip() or self.reasoning_effort not in ("low", "high", "max"):
            raise ValueError("topic生成模型名称或思考强度不合法")
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("topic模型超时必须为有限正数")
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise ValueError("topic模型输出额度必须为正整数")


@dataclass(frozen=True, slots=True)
class ObjectRecallConfig:
    sparse_weight: float = 0.7
    dense_weight: float = 0.3
    rrf_k: int = 60  # 本项目公式按1开始的排名计算
    rrf_limit: int = 15  # 分区合并后保留的粗排候选数，先于SQLite核对与rerank
    limit: int = 10  # 每个新object分别保留最多10个staged及10个linked导航
    fetch_limit: int = 25  # STAGED、LINKED分别按名称/内容的dense、sparse各取该数量
    pools: tuple[RecallTier, ...] = (RecallTier.HOT,)  # 常规整合只查热池；冷池由后续补扫显式指定
    min_rerank_score: float = 0.15
    cliff_ratio: float = 0.5  # 后项不超过前项的一半
    cliff_score_guard: float = 0.3  # 后项必须严格低于该值，才允许切刀

    def __post_init__(self) -> None:
        weights = (self.sparse_weight, self.dense_weight)
        if any(not isfinite(value) or value < 0 for value in weights) or not any(weights):
            raise ValueError("混合召回权重必须非负且不能全为0")
        if (any(type(value) is not int or value < 1 for value in (self.limit, self.fetch_limit, self.rrf_limit))
                or self.limit > self.fetch_limit or self.limit > self.rrf_limit
                or type(self.rrf_k) is not int or self.rrf_k < 0):
            raise ValueError("object召回数量或RRF参数不合法")
        if not self.pools or len(set(self.pools)) != len(self.pools) or any(pool not in tuple(RecallTier) for pool in self.pools):
            raise ValueError("object召回需要有效且不重复的池范围")
        if not isfinite(self.min_rerank_score) or not 0 <= self.min_rerank_score <= 1:
            raise ValueError("rerank门槛必须在0到1之间")
        if (not isfinite(self.cliff_ratio) or not 0 < self.cliff_ratio < 1
                or not isfinite(self.cliff_score_guard) or not 0 <= self.cliff_score_guard <= 1):
            raise ValueError("断崖相对阈值必须在0到1之间，分数护栏必须在0到1范围内")


@dataclass(frozen=True, slots=True)
class TopicRecallConfig(ObjectRecallConfig):
    """Topic召回独立配置；沿用相同的混合权重、热池和后置筛选规则。"""


@dataclass(frozen=True, slots=True)
class MemoryRerankConfig:
    model_path: str = "models/qwen3-reranker-0.6b"
    device: str = "cuda:0"
    batch_size: int = 4
    max_length: int = 2048

    def __post_init__(self) -> None:
        if not self.model_path.strip() or not self.device.strip():
            raise ValueError("reranker需要本地模型路径和设备")
        if any(type(value) is not int or value < 1 for value in (self.batch_size, self.max_length)):
            raise ValueError("reranker批大小和输入容量必须为正整数")
