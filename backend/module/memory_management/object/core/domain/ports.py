"""Object 提取链的持久化能力接口。

约定封存来源加载、提取结果保存、索引阶段登记及正式对象激活，并复用公共任务调度契约。
接口以领域类型传递固定来源、对象和取舍说明，由 object/repository/sqlite.py 实现事务操作。
"""
from __future__ import annotations
from module.memory_management.object.core.domain.job import ObjectJob
from module.memory_management.object.core.domain.document import MemoryObject
from module.memory_management.object.core.domain.generation import ObjectSelectionNote
from module.memory_management.object.core.domain.source import ObjectSourceInput
from module.memory_management.shared.jobs.core.domain.ports import MemoryJobRepository
from typing import Protocol


class ObjectProductionRepository(MemoryJobRepository, Protocol):
    """封存原文→object候选→索引→激活，不读取daily。"""
    async def read_object_source(self, job: ObjectJob) -> ObjectSourceInput: ...
    async def begin_generate(self, job_id: str) -> ObjectJob | None: ...
    async def save_extraction_result(self, job_id: str, attempt_version: int,
                             objects: tuple[MemoryObject, ...], *, source: ObjectSourceInput,
                             selection_notes: tuple[ObjectSelectionNote, ...] = ()) -> bool: ...
    async def begin_index(self, job_id: str) -> ObjectJob | None: ...
    async def save_index(self, job_id: str, attempt_version: int) -> bool: ...
    async def activate_revision(self, job_id: str, attempt_version: int) -> bool:
        """原子写入正式object并置DONE；重复激活不重复发布或覆盖后续topic状态。"""
        ...
