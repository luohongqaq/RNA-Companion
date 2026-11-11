"""按角色组装读取服务，复用调用方持有的连接和编码器。"""

from module.memory_recall.retrieval.core.processing.service import MemoryReadService
from module.memory_recall.retrieval.repository.qdrant import QdrantMemorySearchRepository
from module.memory_recall.retrieval.repository.sqlite import SQLiteMemoryDocumentRepository
from storage.sqlite.db_session import SQLiteDatabase


class RecallReaderFactory:
    def __init__(self, client, encoder=None, *, database: SQLiteDatabase, token_counter=None) -> None:
        self._client = client
        self._encoder = encoder
        self._database = database
        self._token_counter = token_counter

    def create(self, character_id: str) -> MemoryReadService:
        return MemoryReadService(
            SQLiteMemoryDocumentRepository(self._database),
            QdrantMemorySearchRepository(self._client, self._encoder),
            character_id, self._token_counter,
        )
