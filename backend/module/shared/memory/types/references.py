"""memory_record.references: cohesive domain types; no external I/O."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MemoryKind(StrEnum):
    """长期记忆文档类型"""
    DAILY = "daily"
    TOPIC = "topic"
    OBJECT = "object"
    DIARY = "diary"
    IMPRESSION = "impression"
    SHARED = "shared"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    session_id: str
    message_id: str
    quote: str
    turn_id: str | None = None
    source_revision: int | None = None
    source_hash: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryRevisionRef:
    """生成manifest共用的版本引用；hash针对规范化正文，不含向量或导出元数据。"""
    kind: MemoryKind
    memory_id: str  # 对应document_id，保留生成模块的现有引用接口
    revision: int
    content_hash: str
