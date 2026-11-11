"""跨链任务处理与持久化调度接口。

MemoryJobProcessor 约定单次任务处理入口，MemoryJobRepository 约定登记、扫描、重试、失败与执行态恢复。
具体事务与外部资源访问由实现层承担；种子正文发布接口归 shared/indexing/core/domain/ports.py。
"""
from __future__ import annotations
from datetime import datetime
from module.memory_management.shared.jobs.core.domain.command import CreateMemoryJob
from module.memory_management.shared.jobs.core.domain.job import MemoryJob, MemoryJobStatus
from module.memory_management.shared.contracts.source import SealedSessionSource
from typing import Protocol


class MemoryJobProcessor(Protocol):
    async def process_job(self, job_id: str) -> None: ...


class MemoryJobRepository(Protocol):
    """共用调度接口；具体仓储限定自己的任务链。"""
    async def create_job(self, command: CreateMemoryJob) -> str:
        """按dedupe_key幂等登记；具体来源规则由各链实现。"""
        ...

    async def get_unregister_sealed_sessions(self, limit: int = 32) -> list[str]:
        """按封存顺序返回本链尚未完成登记的session ID。"""
        ...

    async def read_session_character_id(self, session_id: str) -> str:
        """读取封存session的角色归属，不加载正文。"""
        ...

    async def read_sealed_session_source(self, session_id: str) -> SealedSessionSource:
        """按轮序读取封存session全部完整原文，不分日期或展开继承材料。"""
        ...

    async def get_job(self, job_id: str) -> MemoryJob | None: ...

    async def get_next_ready_job(self) -> MemoryJob | None:
        """取到期队首；WAITING_INDEX不执行但仍阻塞同目标后续任务。"""
        ...

    async def retry_job(self, job_id: str, attempt_version: int, *,
                        expected_status: MemoryJobStatus, available_at: datetime, error: str,
                        restart_generation: bool = False, expected_retry_count: int | None = None) -> bool:
        """核对阶段/代次/次数；临时异常保留产物退避，来源变化清除失效计划重新生成。"""
        ...

    async def fail_job(self, job_id: str, attempt_version: int, error: str, *,
                       expected_status: MemoryJobStatus, expected_retry_count: int | None = None) -> bool: ...

    async def recover_unfinished_attempts(self) -> int:
        """旧协程结束后恢复遗留执行态；已提交的昂贵操作结果不回退。"""
        ...


