"""应用共享的 SQLite、Qdrant、BGE 与记忆重排资源。"""
from __future__ import annotations
import asyncio
from pathlib import Path
from module.memory_management.shared.indexing.repository.qdrant import QdrantMemoryIndexRepository
from module.memory_management.topic.core.processing.config import MemoryRerankConfig
from module.memory_management.topic.repository.rerank import QwenMemoryRerankRepository
from module.memory_management.topic.repository.retrieval import QdrantMemoryRecallRepository
from module.memory_recall.shared.core.domain.errors import MemoryRecallUnavailable
from qdrant_client import AsyncQdrantClient
from storage.config.settings import Settings
from storage.sqlite.db_session import SQLiteDatabase


class MemoryStorageRuntime:
    """统一初始化和释放存储，仓储借用这里创建的资源。"""
    def __init__(self, settings: Settings, *, database_path: Path | None = None) -> None:
        self._settings = settings
        self.database = SQLiteDatabase(database_path or settings.data_root / "rna_companion.db")
        self._qdrant = None
        self._encoder = None
        self._index = None
        self._production_recall = None
        self._reranker = None

    async def start(self, *, initialize_database: bool = True) -> None:
        if self._qdrant is not None:
            return
        try:
            if initialize_database:
                await self.database.initialize()
            if not (self._settings.memory_enabled or self._settings.memory_production_enabled):
                return
            if self._settings.memory_qdrant_path is None:
                raise ValueError("请配置RNA_MEMORY_QDRANT_PATH")
            self._qdrant = AsyncQdrantClient(path=str(self._settings.memory_qdrant_path))
            if self._settings.memory_embedding_path is not None:
                self._encoder = await asyncio.to_thread(QdrantMemoryIndexRepository.load_encoder,
                                                       str(self._settings.memory_embedding_path))
                await asyncio.to_thread(QdrantMemoryIndexRepository.encode_dual_vectors, ["预热"], self._encoder)
                self._index = QdrantMemoryIndexRepository(self._qdrant, self._encoder)
                self._production_recall = QdrantMemoryRecallRepository(self._qdrant, self._encoder,
                                                                       index_repository=self._index)
            if self._settings.memory_production_enabled:
                if self._index is None:
                    raise ValueError("记忆生产缺少本地BGE-M3")
                await self._index.initialize()
                self._reranker = await QwenMemoryRerankRepository.load(MemoryRerankConfig(
                    model_path=str(self._settings.memory_reranker_path), device=self._settings.memory_reranker_device,
                    batch_size=self._settings.memory_reranker_batch_size,
                    max_length=self._settings.memory_reranker_max_length))
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        try:
            try:
                if self._reranker is not None:
                    await asyncio.to_thread(self._reranker.close)
            finally:
                try:
                    if self._qdrant is not None:
                        await self._qdrant.close()
                finally:
                    await self.database.close()
        finally:
            self._qdrant = self._encoder = self._index = None
            self._production_recall = None
            self._reranker = None

    def get_client(self):
        if self._qdrant is None:
            raise MemoryRecallUnavailable("记忆Qdrant尚未启动")
        return self._qdrant

    def get_encoder(self):
        if self._encoder is None:
            raise MemoryRecallUnavailable("记忆BGE编码器尚未加载")
        return self._encoder

    def get_index_repository(self):
        if self._index is None:
            raise MemoryRecallUnavailable("记忆索引持久层尚未初始化")
        return self._index

    def get_production_recall_repository(self):
        if self._production_recall is None:
            raise MemoryRecallUnavailable("记忆生产召回持久层尚未初始化")
        return self._production_recall

    def get_rerank_repository(self):
        if self._reranker is None:
            raise MemoryRecallUnavailable("记忆object重排模型尚未加载")
        return self._reranker
