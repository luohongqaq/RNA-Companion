"""检索、正式正文读取与单次材料账本的能力接口。

文档仓储、语义检索和 MemoryReadPort 分别约定正式版本读取、候选搜索与工具业务操作。
ReadLedger 统一候选和已读句柄登记；ReadSessionSeed 只提供跨轮正文材料，避免读取会话依赖具体缓存类型。
"""
from __future__ import annotations
from module.shared.memory.types.references import MemoryRevisionRef
from module.memory_recall.retrieval.core.domain.document import RecallDocument
from module.memory_recall.retrieval.core.domain.retrieval import RecallHit
from module.memory_recall.retrieval.core.domain.tools.listing import ListInput, ListResult
from module.memory_recall.retrieval.core.domain.tools.read import ReadInput, ReadResult
from module.memory_recall.retrieval.core.domain.tools.search import SearchInput, SearchResult
from module.memory_recall.shared.core.domain.records import ReadRecord, RecentRecord
from typing import Any, Protocol


class ReadLedger(Protocol):
    """读取器签发引用和固定已读材料需要的最小业务账本能力。"""
    cards: dict[str, str]
    read_set: dict[str, ReadRecord]

    def register_candidate(self, document_id: str) -> str: ...
    def register_read(self, document_id: str, sections: dict[str, Any], revision: int,
                      content_hash: str, kind: str = "", title: str = "",
                      conversation_time: str | None = None, time_context: dict | None = None) -> str: ...
    def record_retrievals(self, entries: list[RecentRecord]) -> None: ...


class MemoryReadPort(Protocol):
    async def load_records(self, document_ids: list[str]) -> dict[str, ReadRecord]:
        """按ID读取并核验最新正文；不执行模型工具循环。"""
        ...
    async def has_documents(self) -> bool:
        """快照内是否存在任何正式文档；空库时调用方可短路选择循环。"""
        ...
    async def temporal_coverage(self) -> dict[str, Any]: ...
    async def search(self, query: SearchInput, session: ReadLedger) -> SearchResult: ...
    async def read(self, query: ReadInput, session: ReadLedger) -> ReadResult: ...
    async def list_memories(self, query: ListInput, session: ReadLedger) -> ListResult: ...


class MemoryReaderFactory(Protocol):
    def create(self, character_id: str) -> MemoryReadPort:
        """每个attempt独立读取器/时间快照，复用连接和编码器。"""
        ...


class MemoryDocumentRepository(Protocol):
    async def load(self, character_id: str) -> tuple[RecallDocument, ...]:
        """一个读快照加载角色当前已发布的冷热池文档；核对完整正文hash。"""
        ...


class MemorySearchRepository(Protocol):
    async def search(self, query: str, *, character_id: str,
                     refs: tuple[MemoryRevisionRef, ...], limit: int) -> tuple[RecallHit, ...]:
        """仅检索固定正式版本的分区，聚合为去重文档；不提供正式正文。"""
        ...


class ReadSessionSeed(Protocol):
    """提供跨轮材料供读取会话恢复；不要求调用方使用具体缓存类。"""
    recent_update_memory: list[ReadRecord]
    recent_retrievals: list[ReadRecord]
