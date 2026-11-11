"""jobs.models: cohesive domain types; no external I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Generic, TypeVar


class JobFamily(StrEnum):
    """任务家族；用于调度时快速判断任务类型。"""
    GENERATION = "generation"
    CONTEXT = "context"
    MEMORY = "memory_record"
    STORAGE = "storage"


class WorkerPool(StrEnum):
    """工作池；用于限制任务并发。"""
    FOREGROUND_LLM = "foreground_llm"
    CONTEXT_LLM = "context_llm"
    MEMORY_LLM = "memory_llm"
    STORAGE = "storage"


class JobPriority(IntEnum):
    """任务优先级；数字越小越优先。"""
    HANDOFF_BLOCKING = 0
    CONTEXT_URGENT = 5
    CONTEXT = 10
    MEMORY = 20
    PERSONAL_MEMORY = 25
    MAINTENANCE = 40
    CLEANUP = 50


class DispatchBlockReason(StrEnum):
    """仅用于一次调度判断，不持久化为任务status。"""
    STATUS_NOT_EXECUTABLE = "status_not_executable"
    NOT_AVAILABLE = "not_available"
    DEPENDENCY_NOT_READY = "dependency_not_ready"
    EARLIER_TARGET_JOB = "earlier_target_job"
    ALREADY_INFLIGHT = "already_inflight"
    TARGET_BUSY = "target_busy"
    POOL_FULL = "pool_full"


type JobStatus = str


type JobType = str


@dataclass(frozen=True, slots=True)
class JobRef:
    """跨任务表引用；不是新建一张通用jobs表。"""
    family: JobFamily
    job_id: str


@dataclass(frozen=True, slots=True)
class JobScheduleInfo:
    """从具体任务表查询得到的只读调度视图

    必须先检查状态、available_at、依赖、同目标队首、内存互斥和资源池，
    再按(priority, created_at)排序；并列时可用job_id作稳定排序。
    Generation的target_key可投影为turn:<turn_id>。
    """
    ref: JobRef
    job_type: JobType
    target_key: str
    status: JobStatus
    priority: int
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    retry_count: int = 0


@dataclass(frozen=True, slots=True)
class JobExecutionTicket:
    """一次阶段执行的内存票据
    worker提交时重新校验expected_status及领域版本/hash，不能只信此快照。
    """
    ref: JobRef
    expected_status: JobStatus
    worker_pool: WorkerPool
    target_key: str


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    """阻塞原因用于诊断；只有实际调度器能原子登记inflight并占用槽位。"""
    ref: JobRef
    worker_pool: WorkerPool
    block_reasons: tuple[DispatchBlockReason, ...] = ()  # 空表示本次判断可执行
    blocking_job: JobRef | None = None


@dataclass(slots=True)
class SchedulerState:
    """仅存于当前进程，重启清空；登记/释放须由调度器原子执行。

    inflight_jobs保留槽位归属，inflight_job_ids与active_target_keys按文档排重。
    释放执行互斥后，waiting_index的父任务仍受数据库队首规则保护。
    """
    inflight_job_ids: set[str] = field(default_factory=set)
    active_target_keys: set[str] = field(default_factory=set)
    inflight_jobs: dict[JobRef, JobExecutionTicket] = field(default_factory=dict)
    pool_usage: dict[WorkerPool, int] = field(default_factory=dict)


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class StageResult(Generic[T]):
    """worker交给存储端的阶段提交材料，不表示已经COMMIT成功。

    整份产物与next_status同事务提交；父子任务切换由对应存储操作原子处理。
    """
    ref: JobRef
    expected_status: JobStatus
    next_status: JobStatus
    artifact: T


@dataclass(frozen=True, slots=True)
class JobRecoveryReport:
    """启动扫描分类；按已提交阶段恢复，不把任务重置到初始阶段。"""
    ready_jobs: tuple[JobRef, ...] = ()
    waiting_jobs: tuple[JobRef, ...] = ()  # 退避或等待依赖，不立即执行
    obsolete_jobs: tuple[JobRef, ...] = ()
    failed_jobs: tuple[JobRef, ...] = ()
