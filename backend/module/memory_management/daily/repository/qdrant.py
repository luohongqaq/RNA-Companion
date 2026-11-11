"""Daily 索引持久化入口，继承共用的 Qdrant 编码和写入实现。"""
from module.memory_management.shared.core.domain.document import VectorDocument
from module.shared.memory.types.references import MemoryKind
from module.memory_management.shared.indexing.core.domain.representation import IndexRepresentation
from module.memory_management.shared.indexing.repository.qdrant import QdrantMemoryIndexRepository


class DailyIndexRepository(QdrantMemoryIndexRepository):
    """写入 daily 正文分区；连接、编码器和并发锁由公共仓储共享。"""

    async def batch_hybrid_index(self, document: VectorDocument,
                                 representations: tuple[IndexRepresentation, ...]) -> bool:
        if document.ref.kind != MemoryKind.DAILY:
            raise ValueError("Daily 索引仓储只接受 daily 文档")
        return await super().batch_hybrid_index(document, representations)
