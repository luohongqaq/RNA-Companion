"""记忆管理的公共索引写入与元数据同步接口。

MemoryIndexRepository 约定正文分区混合索引、object 批量索引，以及 object 状态、冷热池和 topic 关联同步。
SeedPublicationPort 约定种子版本查询和正式正文发布，供索引确认后的种子导入使用。
接口只使用领域文档与表示类型，具体编码模型和 Qdrant 客户端封装在 shared/indexing/repository。
"""
from __future__ import annotations
from collections.abc import Sequence
from module.memory_management.shared.core.domain.document import VectorDocument
from module.memory_management.shared.indexing.core.domain.representation import IndexRepresentation
from module.memory_management.object.core.domain.document import MemoryObject
from typing import Protocol


class MemoryIndexRepository(Protocol):
    """BGE-M3向量化与批量索引写入，不依赖SQLite或缓存向量。"""
    async def batch_hybrid_index(self, document: VectorDocument,
                                 representations: tuple[IndexRepresentation, ...]) -> bool: ...

    async def batch_object_index(self, objects: tuple[MemoryObject, ...]) -> bool:
        """批量编码并写入object专用集合；相同object_id重试覆盖。"""
        ...

    async def update_object_metadata(self, objects: tuple[MemoryObject, ...]) -> bool:
        """按SQLite当前记录同步两个分区的状态、冷热池与topic关联，不重新编码。"""
        ...


class SeedPublicationPort(Protocol):
    async def current_versions(self, document_ids: Sequence[str], character_id: str) -> dict[str, tuple[int, str]]: ...

    async def publish_seed(self, documents: Sequence[dict], character_id: str, namespace: str) -> dict[str, int]:
        """登记已完成索引的结构化测试文档；不伪造原始聊天消息。"""
        ...
