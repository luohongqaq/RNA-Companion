"""记忆管理任务的公共输入、状态和持久化记录。

统一任务家族、阶段状态、调度时间、重试信息、执行代次、来源清单和发布版本字段。
各链任务在 MemoryJob 基础上增加专有产物，公共记录供登记、调度与恢复使用。
"""
from __future__ import annotations
from module.memory_management.shared.core.domain.document import VectorDocument
from module.shared.memory.types.references import MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from module.memory_management.shared.contracts.source import MemorySourceManifest


class MemoryJobType(StrEnum):
    """长期记忆的job类型"""
    CREATE_DAILY = "create_daily"
    EXTRACT_OBJECTS = "extract_objects"
    CONSOLIDATE_TOPIC = "consolidate_topic"
    CREATE_TOPIC = "create_topic"
    UPDATE_TOPIC = "update_topic"
    USER_EDIT = "user_edit"


class MemoryJobStatus(StrEnum):
    """长期记忆的job状态"""
    NEED_GENERATE = "need_generate"
    GENERATING = "generating"  # 已登记执行代次；计划和来源随生成候选一起保存
    NEED_REVIEW = "need_review"
    REVIEWING = "reviewing"  # 已读取候选并登记审核代次，拒绝重复begin_review
    NEED_WRITE = "need_write"  # 准备结构化待发布正文
    WRITING = "writing"  # 已登记写入代次，拒绝重复begin_write
    INDEXING = "indexing"  # Qdrant编码与写入中，失败重新索引
    NEED_INDEX = "need_index"  # 待发布正文已保存，尚未登记索引任务
    WAITING_INDEX = "waiting_index"
    NEED_ACTIVATE = "need_activate"
    DONE = "done"
    OBSOLETE = "obsolete"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MemoryJobInput:
    """各链任务共有的身份和目标版本信息；实际使用具体子类。"""
    character_id: str
    source_session_ids: tuple[str, ...]
    target_id: str | None = None
    base_revision: int | None = None  # 用户提交编辑时，客户端所读过的那份记忆的版本号；自动生成链路不填
    base_content_hash: str | None = None
    target_revision: int | None = None
    recall_tier: RecallTier = RecallTier.HOT


@dataclass(frozen=True, slots=True)
class CreateMemoryJob:
    """固定登记所需的任务类型、目标键、幂等键和输入。"""
    job_type: MemoryJobType
    target_key: str
    dedupe_key: str
    priority: int
    input: MemoryJobInput


@dataclass(slots=True)
class MemoryJob:
    """公共调度、尝试及发布字段；实例使用DailyJob、ObjectJob或TopicJob。"""
    job_id: str
    job_type: MemoryJobType
    target_key: str  #目标键：job_type:{character_id}:{日期}	要更新哪一天、哪个角色的 job_type
    dedupe_key: str #幂等键： job:{character_id}:{session_id}:{日期}	哪个 session 向哪一天提供了一次材料
    priority: int #调度优先级
    status: MemoryJobStatus  #任务状态
    available_at: datetime  #可用时间：计划开始时间
    input: MemoryJobInput  #输入：身份和目标版本信息
    created_at: datetime  #创建时间：登记时间
    updated_at: datetime  #更新时间：状态更新时间
    schema_version: int = 6  # 当前 object/result/entry JSON 协议
    retry_count: int = 0  #重试次数
    vector_document: VectorDocument | None = None  #待向量化文档内容
    storage_job_id: str | None = None
    result: MemoryRevisionRef | None = None #job的最终结果的版本引用
    last_error: str | None = None # 错误信息
    finished_at: datetime | None = None  #完成时间
    reviewed_version: int | None = None  #审核候选版本
    reviewed_hash: str | None = None  #审核候选内容哈希
    applied_revision: int | None = None  #应用的修订版本
    error_code: str | None = None  #错误代码
    attempt_version: int = 0  #尝试版本
    attempt_count: int = 0  #尝试次数
    source_manifest: MemorySourceManifest | None = None #本次处理的源信息清单

    @property
    def publication_started(self) -> bool:
        return self.result is not None or self.applied_revision is not None
