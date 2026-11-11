"""Topic 关联检索与重排过程的数据契约。

定义 object、topic 的候选命中、检索分组、重排输入输出，以及携带已核对正文的关联结果和 LINKED 导航线索。
保留来源 object、命中分区、排名、分数和截断信息，供 related_retrieval 组织业务筛选，本文件不执行外部检索。
"""
from __future__ import annotations
from dataclasses import dataclass
from math import isfinite
from module.memory_management.object.core.domain.document import MemoryObject
from typing import Literal, TYPE_CHECKING


if TYPE_CHECKING:
    from module.memory_management.topic.core.domain.source import TopicContext


@dataclass(frozen=True, slots=True)
class ObjectRecallHit:
    source_object_id: str
    object_id: str
    rank: int
    score: float
    paths: tuple[str, ...]  # name/content
    rerank_score: float | None = None  # 独立于RRF score，最终rank可按此分数重排
    name_rerank_score: float | None = None
    content_rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class ObjectRerankPair:
    source_object_id: str
    object_id: str
    query: str
    document: str
    section_key: Literal["name", "content"] = "content"


@dataclass(frozen=True, slots=True)
class ObjectRerankScore:
    source_object_id: str
    object_id: str
    score: float
    section_key: Literal["name", "content"] = "content"

    def __post_init__(self) -> None:
        if not isfinite(self.score) or not 0 <= self.score <= 1:
            raise ValueError("rerank分数必须为0到1的有限数值")


@dataclass(frozen=True, slots=True)
class ObjectRecallGroup:
    source_object_id: str
    hits: tuple[ObjectRecallHit, ...]
    truncated: bool = False  # 有一路触及取数上限，不能宣称已经找全


@dataclass(frozen=True, slots=True)
class RelatedObjectRecall:
    source_object_id: str
    pending_objects: tuple[MemoryObject, ...]
    hits: tuple[ObjectRecallHit, ...]  # 保留新object到历史object的命中关系
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class LinkedTopicRecall:
    """通过已关联object得到的topic导航，不包含待整合object。"""
    source_object_id: str
    linked_topic_ids: tuple[str, ...]
    hits: tuple[ObjectRecallHit, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class TopicSearchHit:
    """单个分区的索引命中；SQLite正式版本核对前不视为可用档案。"""
    source_object_id: str
    topic_id: str
    revision: int
    section_key: str
    point_id: str
    rank: int
    score: float


@dataclass(frozen=True, slots=True)
class TopicSearchGroup:
    source_object_id: str
    hits: tuple[TopicSearchHit, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class TopicRerankPair:
    source_object_id: str
    topic_id: str
    query: str
    document: str


@dataclass(frozen=True, slots=True)
class TopicRerankScore:
    source_object_id: str
    topic_id: str
    score: float

    def __post_init__(self) -> None:
        if not isfinite(self.score) or not 0 <= self.score <= 1:
            raise ValueError("topic重排分数必须为0到1的有限数值")


@dataclass(frozen=True, slots=True)
class TopicRecallHit:
    source_object_id: str
    topic_id: str
    rank: int
    rrf_score: float | None
    rerank_score: float
    sections: tuple[str, ...] = ()
    linked: bool = False


@dataclass(frozen=True, slots=True)
class RelatedTopicRecall:
    source_object_id: str
    topics: tuple[TopicContext, ...]
    hits: tuple[TopicRecallHit, ...]
    truncated: bool = False
