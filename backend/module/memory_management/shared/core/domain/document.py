"""正式日记忆和主题版本，以及面向索引的结构化文档。"""
from __future__ import annotations

from module.memory_management.daily.core.domain.document import DailyDocument
from module.shared.memory.types.references import EvidenceRef, MemoryKind, MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier
from module.memory_management.topic.core.domain.document import TopicDocument
from module.shared.memory.types.indexing import PersonRef
from dataclasses import dataclass
from datetime import date, datetime


type StructuredMemoryDocument = DailyDocument | TopicDocument


@dataclass(frozen=True, slots=True)
class MemoryDocument:
    """SQLite当前已发布版本登记；生成和索引进度由任务管理。"""
    document_id: str
    kind: MemoryKind
    current_revision: int
    content_hash: str
    vector_document_key: str
    published_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyMemory(MemoryDocument):
    memory_date: date
    source_session_ids: tuple[str, ...]
    document: DailyDocument

    def __post_init__(self) -> None:
        if (self.document.memory_date != self.memory_date
                or self.document.source_session_ids != self.source_session_ids):
            raise ValueError("daily正文与日期或来源session登记不一致")


@dataclass(frozen=True, slots=True, kw_only=True)
class TopicMemory(MemoryDocument):
    source_refs: tuple[EvidenceRef, ...]
    document: TopicDocument


@dataclass(frozen=True, slots=True)
class MemoryDocumentContent:
    """按ID+revision从向量数据库获取并核验SQLite版本与正文hash后的材料。"""
    ref: MemoryRevisionRef
    character_id: str
    vector_document_key: str
    document: StructuredMemoryDocument
    recall_tier: RecallTier


@dataclass(frozen=True, slots=True)
class VectorDocument:
    """完整版本正文；检索表示可以有多个，但不能再生成一份搜索专用记忆正文。"""
    vector_document_key: str
    ref: MemoryRevisionRef
    character_id: str
    document: StructuredMemoryDocument
    people: tuple[PersonRef, ...]
    topics: tuple[str, ...]  # 规范主题；与原始查询主题分开
    preview_text: str
    session_started_at: datetime | None = None
    session_ended_at: datetime | None = None
