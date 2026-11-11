from __future__ import annotations
from module.shared.memory.types.indexing import IndexHit
from module.memory_management.object.core.domain.document import MemoryObject
from module.memory_management.topic.core.domain.retrieval import (
    ObjectRecallGroup,
    ObjectRerankPair,
    ObjectRerankScore,
    TopicRerankPair,
    TopicRerankScore,
    TopicSearchGroup,
)
from module.memory_management.topic.core.processing.config import (
    ObjectRecallConfig,
    TopicModelConfig,
    TopicRecallConfig,
)
from typing import Protocol


class RelatedMemoryRerankPort(Protocol):
    async def batch_topic_rerank(self, pairs: tuple[TopicRerankPair, ...]) -> tuple[TopicRerankScore, ...]:
        """每个新object-topic候选对只评估一次，不按分区重复打分。"""
        ...
    async def batch_object_rerank(self, pairs: tuple[ObjectRerankPair, ...]) -> tuple[ObjectRerankScore, ...]:
        """批量评估文本对，返回可按来源ID、候选ID关联的分数，不执行limit截断。"""
        ...


class RelatedMemorySearchPort(Protocol):
    """分区混合检索，与索引写入接口分开。"""
    async def batch_topic_search(self, objects: tuple[MemoryObject, ...], *,
                                 config: TopicRecallConfig) -> tuple[TopicSearchGroup, ...]:
        """只检索topic名称、身份、当前状态，保留来源object与分区。"""
        ...
    async def hybrid_search(self, query: str, *, character_id: str, limit: int = 10) -> tuple[IndexHit, ...]:
        """返回候选引用，业务层须核对SQLite当前生效版本。"""
        ...

    async def batch_object_search(self, objects: tuple[MemoryObject, ...], *,
                                  config: ObjectRecallConfig) -> tuple[ObjectRecallGroup, ...]:
        """只检索STAGED object；业务层须核对SQLite当前状态。"""
        ...

    async def batch_linked_topic_search(self, objects: tuple[MemoryObject, ...], *,
                                        config: ObjectRecallConfig) -> tuple[ObjectRecallGroup, ...]:
        """独立检索LINKED object，供业务层解析topic导航，不混入object召回。"""
        ...


class TopicModelPort(Protocol):
    async def complete(self, prompt: str, payload: dict, config: TopicModelConfig) -> dict: ...
