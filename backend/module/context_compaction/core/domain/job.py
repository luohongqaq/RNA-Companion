"""context.jobs: cohesive domain types; no external I/O."""
from __future__ import annotations

from module.context_compaction.core.domain.handoff import HandoffPacket
from module.context_compaction.core.domain.plan import CompactionPlan
from module.shared.context.types.references import SnapshotRef
from module.context_compaction.core.domain.candidate import SummaryCandidate, SummaryValidation
from module.context_compaction.core.domain.snapshots import SummarySourceManifest
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ContextJobType(StrEnum):
    """上下文任务类型"""
    BUILD_SNAPSHOT = "build_snapshot" #保留位；统一交接流程只登记PREPARE_HANDOFF
    PREPARE_HANDOFF= "prepare_handoff"


class ContextJobStatus(StrEnum):
    """上下文任务状态"""
    NEED_GENERATE = "need_generate"
    GENERATING = "generating" #已登记执行代次；计划和来源随候选一起保存
    NEED_VALIDATE = "need_validate" #仅执行程序校验，不调用审核模型
    VALIDATING = "validating" #已登记校验代次；保留生成候选
    NEED_PUBLISH = "need_publish"
    READY = "ready" #交接摘要已发布，等待采用；此时不填写finished_at
    DONE = "done" #交接事务已成功切换session
    FAILED = "failed"
    OBSOLETE = "obsolete" #来源修订变化或任务已被替代


@dataclass(slots=True)
class ContextJob:
    """status表示下一步；阶段产物与状态推进在同一事务提交。"""
    job_id: str
    job_type: ContextJobType
    session_id: str
    target_key: str
    dedupe_key: str
    priority: int
    status: ContextJobStatus
    available_at: datetime
    frozen_through_turn_seq: int #登记时固定的来源上界
    created_at: datetime
    updated_at: datetime
    retry_count: int = 0
    plan: CompactionPlan | None = None
    candidate: SummaryCandidate | None = None
    validation: SummaryValidation | None = None
    handoff_packet: HandoffPacket | None = None
    published_snapshot: SnapshotRef | None = None
    last_error: str | None = None
    finished_at: datetime | None = None #交接成功或任务失败/作废时填写，READY时保持None
    attempt_version: int = 0 #阶段启动或中断恢复时递增，作为执行资格版本
    attempt_count: int = 0 #仅统计生成启动次数，校验和恢复不消耗生成额度
    source_manifest: SummarySourceManifest | None = None


@dataclass(frozen=True, slots=True)
class CreateContextJobCommand:
    """登记上下文维护任务的输入；不是持久化任务实体。"""
    job_type: ContextJobType
    session_id: str
    frozen_through_turn_seq: int
    priority: int
    dedupe_key: str

