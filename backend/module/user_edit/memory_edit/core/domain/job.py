"""用户编辑索引的生命周期取值与已领取固定快照。

MemoryEditStatus 区分等待、索引中、完成、失败和被新版本替代；MemoryEdit 固定待编码正文、版本、哈希及执行代次。
后台索引只使用领取快照，重试不再次修改正式正文，迟到执行通过代次和当前版本核对隔离。
"""
from __future__ import annotations
from module.memory_management.shared.core.domain.document import StructuredMemoryDocument
from module.shared.memory.types.retention import RecallTier
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from module.user_edit.memory_edit.core.domain.query import DocumentKind


class MemoryEditStatus(StrEnum):
    PENDING = "pending"
    INDEXING = "indexing"
    DONE = "done"
    FAILED = "failed"
    OBSOLETE = "obsolete"  # 已被更新的正式版本取代，不再索引


@dataclass(frozen=True, slots=True)
class MemoryEdit:
    """已领取的固定版本；attempt_version 防止迟到协程修改新执行的状态。"""
    edit_id: str
    character_id: str
    document_id: str
    kind: DocumentKind
    revision: int
    content_hash: str
    document: StructuredMemoryDocument
    recall_tier: RecallTier
    attempt_version: int
    retry_count: int
    created_at: datetime
