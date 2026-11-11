"""Topic 整合链的持久化能力接口。

约定正式 object 与 topic 核对、封存会话批次登记、固定来源读取，以及生成、审核、发布各阶段产物保存。
索引完成后通过激活接口切换正式版本；事务、执行代次和当前版本核对由 topic/repository/sqlite.py 实现。
"""
from __future__ import annotations
from module.memory_management.shared.core.domain.document import VectorDocument
from module.shared.memory.types.retention import RecallTier
from module.memory_management.object.core.domain.document import MemoryObject
from module.memory_management.shared.jobs.core.domain.command import CreateMemoryJob
from module.memory_management.shared.jobs.core.domain.ports import MemoryJobRepository
from module.memory_management.topic.core.domain.source import TopicContext
from module.memory_management.topic.core.domain.generation import TopicBatchResult
from module.memory_management.topic.core.domain.job import TopicJob
from module.memory_management.topic.core.domain.publication import TopicWriteItem
from module.memory_management.topic.core.domain.review import TopicReviewResult
from module.memory_management.topic.core.domain.source import TopicSourceInput
from typing import Protocol


class TopicProductionRepository(MemoryJobRepository, Protocol):
    """object候选→单入口topic整合→审核、写入、索引、激活。"""
    async def read_topics(self, topic_ids: tuple[str, ...], *, character_id: str,
                          pools: tuple[RecallTier, ...]) -> tuple[TopicContext, ...]:
        """读取当前已发布、角色和池范围匹配的正式正文及版本。"""
        ...
    async def read_objects(self, object_ids: tuple[str, ...], *, character_id: str) -> tuple[MemoryObject, ...]:
        """批量核对正式object；缺失或其他角色的向量候选不返回。"""
        ...
    async def read_sealed_session_objects(self, session_id: str) -> tuple[MemoryObject, ...]:
        """按已完成提取任务的固定ID读取正式object，允许空结果。"""
        ...

    async def register_topic_jobs_for_session(self, session_id: str,
                                              commands: tuple[CreateMemoryJob, ...]) -> tuple[str, ...]:
        """同事务登记完整批次并写session首次完成标记。"""
        ...
    async def read_topic_source(self, job: TopicJob) -> TopicSourceInput: ...
    async def begin_generate(self, job_id: str) -> TopicJob | None: ...
    async def save_generation_result(self, job_id: str, attempt_version: int, generation_result: TopicBatchResult, *,
                             source: TopicSourceInput) -> bool:
        """批次计划、固定来源和多份申请一起保存。"""
        ...
    async def begin_review(self, job_id: str) -> TopicJob | None: ...
    async def save_review(self, job_id: str, attempt_version: int, review: TopicReviewResult) -> bool: ...
    async def begin_write(self, job_id: str) -> TopicJob | None: ...
    async def save_write(self, job_id: str, attempt_version: int, results: tuple[TopicWriteItem, ...]) -> bool: ...
    async def begin_index(self, job_id: str) -> TopicJob | None: ...
    async def save_index(self, job_id: str, attempt_version: int, documents: tuple[VectorDocument, ...]) -> bool: ...
    async def prepare_activation(self, job_id: str, attempt_version: int) -> TopicJob | None: ...
    async def activate_revision(self, job_id: str, attempt_version: int) -> bool: ...
