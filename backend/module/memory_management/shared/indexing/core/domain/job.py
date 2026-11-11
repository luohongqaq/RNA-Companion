"""索引任务输入、执行阶段与持久化检查点。

StorageJobInput 固定文档版本、待索引正文、表示和编码模型版本；StorageJob 保存编码、写入、核验及重试信息。
通过父任务引用关联记忆发布流程，供调度和恢复使用；编码与数据库状态更新不在本文件执行。
"""
from __future__ import annotations
from module.memory_management.shared.core.domain.document import VectorDocument
from module.shared.memory.types.references import MemoryRevisionRef
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from module.memory_management.shared.indexing.core.domain.embedding import EmbeddingArtifact
from module.memory_management.shared.indexing.core.domain.representation import IndexRepresentation
from module.memory_management.shared.indexing.core.domain.verification import IndexVerification


class StorageJobType(StrEnum):
    """持久化任务的数据类型"""
    BUILD_INDEX = "build_index"


class StorageJobStatus(StrEnum):
    """持久化过程中的job状态"""
    NEED_EMBED = "need_embed"
    NEED_WRITE = "need_write"
    NEED_VERIFY = "need_verify"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StorageJobInput:
    character_id: str
    ref: MemoryRevisionRef
    vector_document: VectorDocument
    embedding_model_version: str
    representations: tuple[IndexRepresentation, ...]


@dataclass(slots=True)
class StorageJob:
    """成功后与父memory job的need_activate原子提交；永久失败向父传播。"""
    job_id: str
    job_type: StorageJobType
    parent_job_id: str | None
    target_key: str
    dedupe_key: str
    priority: int
    status: StorageJobStatus
    available_at: datetime
    input: StorageJobInput
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1
    retry_count: int = 0
    embedding_artifact: EmbeddingArtifact | None = None
    expected_points: tuple[str, ...] = ()
    verification: IndexVerification | None = None
    last_error: str | None = None
    finished_at: datetime | None = None
