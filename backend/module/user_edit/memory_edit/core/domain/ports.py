"""用户记忆查询、编辑保存、后台任务和索引写入的能力接口。

分别声明查询、原子保存、任务领取与恢复、分区索引写入，避免服务依赖不需要的外部能力。
SQLiteMemoryEditRepository 实现前三类接口以保持事务完整，Qdrant 索引由独立实现承担。
"""
from __future__ import annotations
from module.memory_management.shared.core.domain.document import DailyMemory, TopicMemory
from module.memory_management.shared.core.domain.document import VectorDocument
from datetime import datetime
from module.memory_management.shared.indexing.core.domain.representation import IndexRepresentation
from module.user_edit.memory_edit.core.domain.command import SaveMemoryEdit
from module.user_edit.memory_edit.core.domain.job import MemoryEdit
from module.user_edit.memory_edit.core.domain.query import DailyMemoryCursor, DocumentKind, TopicMemoryCursor
from module.user_edit.memory_edit.core.domain.result import (
    DailyMemoryListItem,
    MemoryDetails,
    MemoryEditDetails,
    MemoryEditResult,
    MemoryListItem,
)
from typing import Protocol


class MemoryQueryRepository(Protocol):
    async def list_daily_memories(
        self, *, character_id: str, limit: int = 20, after: DailyMemoryCursor | None = None,
    ) -> tuple[DailyMemoryListItem, ...]:
        """按memory_date、daily_id升序读取；业务服务可多取一条判断下一页。"""
        ...

    async def list_topic_memories(
        self, *, character_id: str, limit: int = 20, before: TopicMemoryCursor | None = None,
    ) -> tuple[MemoryListItem, ...]:
        """按updated_at、topic_id倒序读取；业务服务可多取一条判断下一页。"""
        ...

    async def get_memory(
        self, *, character_id: str, kind: DocumentKind, document_id: str,
    ) -> MemoryDetails | None:
        """读取所属角色的指定正式正文，不存在或尚未发布时返回None。"""
        ...


class MemoryEditRepository(Protocol):
    async def get_memory_for_edit(self, *, character_id: str, kind: DocumentKind,
                                  document_id: str) -> DailyMemory | TopicMemory | None: ...

    async def save_memory_edit(self, command: SaveMemoryEdit) -> MemoryEditResult:
        """幂等检查、正式正文切版和编辑记录插入在同一事务中完成。"""
        ...

    async def get_memory_edit(self, edit_id: str) -> MemoryEditDetails | None: ...

    async def retry_failed_memory_edit_index(self, edit_id: str) -> MemoryEditDetails:
        """只重开当前正式版本的失败索引，不修改正文或递增文档版本。"""
        ...


class MemoryEditJobRepository(Protocol):
    async def claim_next_memory_edit(self) -> MemoryEdit | None: ...

    async def complete_memory_edit_index(self, edit_id: str, attempt_version: int) -> bool: ...

    async def retry_memory_edit_index(self, edit_id: str, attempt_version: int, *,
                                      available_at: datetime, error: str) -> bool: ...

    async def fail_memory_edit_index(self, edit_id: str, attempt_version: int, *, error: str) -> bool: ...

    async def recover_memory_edit_indexes(self) -> int: ...


class MemoryEditIndexPort(Protocol):
    async def batch_hybrid_index(self, document: VectorDocument,
                                 representations: tuple[IndexRepresentation, ...]) -> bool: ...
