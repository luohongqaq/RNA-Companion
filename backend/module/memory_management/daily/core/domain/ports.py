"""Daily 业务依赖的持久化与索引能力接口。

DailyProductionRepository 约定按会话登记任务、加载固定来源、保存阶段产物和激活正式版本。
DailyIndexPort 约定日记忆分区索引写入；具体 SQLite 与 Qdrant 实现位于 daily/repository。
"""
from __future__ import annotations
from module.memory_management.shared.core.domain.document import VectorDocument
from module.memory_management.daily.core.domain.generation import DailyCandidate
from module.memory_management.daily.core.domain.job import DailyJob
from module.memory_management.daily.core.domain.publication import DailyWrite
from module.memory_management.daily.core.domain.review import DailyReview
from module.memory_management.daily.core.domain.source import DailySourceInput
from module.memory_management.shared.indexing.core.domain.representation import IndexRepresentation
from module.memory_management.shared.jobs.core.domain.command import CreateMemoryJob
from module.memory_management.shared.jobs.core.domain.ports import MemoryJobRepository
from typing import Protocol


class DailyIndexPort(Protocol):
    """Daily 正文分区索引写入契约。"""

    async def batch_hybrid_index(self, document: VectorDocument,
                                 representations: tuple[IndexRepresentation, ...]) -> bool: ...


class DailyProductionRepository(MemoryJobRepository, Protocol):
    """同目标队首串行；阶段产物与状态同事务提交，迟到代次不得覆盖新结果。"""

    async def register_daily_jobs_for_session(self, session_id: str,
                                              commands: tuple[CreateMemoryJob, ...]) -> tuple[str, ...]:
        """后台独立事务登记全部日期任务及完成标记；已完成登记返回空元组。

        空session允许空批次；来源范围不完整或登记失败时整体回滚。
        """
        ...


    async def begin_generate(self, job_id: str) -> DailyJob | None:
        """只登记执行代次并进入GENERATING，不读取来源或保存计划；非到期队首返回None。"""
        ...

    async def begin_review(self, job_id: str) -> DailyJob | None:
        """短事务读取已保存候选、登记代次并进入REVIEWING，保留固定来源。"""
        ...

    async def read_daily_source(self, job: DailyJob) -> DailySourceInput:
        """一次读取材料并在内存中规划；已有候选时按保存的计划核对来源。"""
        ...


    async def save_candidate(self, job_id: str, attempt_version: int,
                             candidate: DailyCandidate, *,
                             source: DailySourceInput | None = None) -> bool:
        """模型返回后原子保存计划、来源、候选和NEED_REVIEW，作为生成恢复点。"""
        ...


    async def save_review(self, job_id: str, attempt_version: int,
                          review: DailyReview, *, retry_rejected: bool = True) -> bool: ...

    async def begin_write(self, job_id: str) -> DailyJob | None:
        """登记WRITING并返回已审核候选，不读取原始对话或写文件。"""
        ...

    async def save_write(self, job_id: str, attempt_version: int, result: DailyWrite) -> bool:
        """原子保存待发布正文与NEED_INDEX；尚不覆盖正式daily。"""
        ...

    async def begin_index(self, job_id: str) -> DailyJob | None:
        """登记INDEXING，保留已审核的待发布正文。"""
        ...

    async def save_index(self, job_id: str, attempt_version: int, document: VectorDocument) -> bool:
        """仅在Qdrant确认写入后保存版本引用，进入NEED_ACTIVATE。"""
        ...


    async def activate_revision(self, job_id: str, attempt_version: int) -> bool:
        """索引就绪后激活结构化正文并完成任务，发布须核对基础版本。"""
        ...
