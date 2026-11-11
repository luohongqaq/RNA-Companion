"""用户编辑后的记忆索引实现入口。

MemoryEditIndexRepository 继承公共分区编码与 Qdrant 写入能力，通过 from_shared 复用客户端、编码器及并发锁。
本实现只用于重建固定编辑快照的索引，正式版本保存和切换由 SQLite 编辑仓储完成。
"""
from module.memory_management.shared.indexing.repository.qdrant import QdrantMemoryIndexRepository


class MemoryEditIndexRepository(QdrantMemoryIndexRepository):
    """为固定编辑快照重建分区索引；正文保存和版本切换由 SQLite 编辑仓储完成。"""
