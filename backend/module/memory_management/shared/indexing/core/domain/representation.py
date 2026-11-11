"""待编码和写入的单个记忆分区表示。

IndexRepresentation 绑定正式版本引用、角色、表示类别、分区键、索引文本、文本哈希和冷热池。
同一表示的稠密与稀疏向量写入同一个 point；实际编码和 Qdrant 写入由索引仓储执行。
"""
from __future__ import annotations
from module.shared.memory.types.indexing import RepresentationKind
from module.shared.memory.types.references import MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IndexRepresentation:
    """同一表示的dense和sparse写入同一个point。"""
    ref: MemoryRevisionRef
    character_id: str
    representation_kind: RepresentationKind
    section_key: str  # Daily全文或Topic身份/当前状态/时间线
    text: str
    text_hash: str
    recall_tier: RecallTier
