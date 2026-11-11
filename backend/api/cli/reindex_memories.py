"""按SQLite正式正文重建当前分区索引；保留业务版本和其他角色数据。"""
from __future__ import annotations
import argparse
import asyncio
from module.memory_management.daily.core.domain.publication import DailyWrite
from module.memory_management.topic.core.domain.publication import TopicWrite
from module.memory_management.shared.core.domain.document import VectorDocument
from module.shared.memory.types.references import MemoryKind
from module.memory_recall.retrieval.repository.sqlite import SQLiteMemoryDocumentRepository
from storage.config.memory_runtime import MemoryStorageRuntime
from storage.config.settings import Settings


async def reindex_character(database, index, character_id: str) -> int:
    documents = await SQLiteMemoryDocumentRepository(database).load(character_id)
    for item in documents:
        write = (DailyWrite if item.ref.kind == MemoryKind.DAILY else TopicWrite).from_document(item.document)
        document = VectorDocument(f"{item.ref.memory_id}:r{item.ref.revision}:{item.ref.content_hash}",
            item.ref, item.character_id, item.document, (), (),
            item.document.title if item.ref.kind == MemoryKind.DAILY else item.document.topic)
        if not await index.batch_hybrid_index(document,
                write.index_representations(item.ref, item.character_id, item.recall_tier)):
            raise RuntimeError("正式记忆分区索引尚未确认完成")
    return len(documents)


async def main(character_id: str) -> None:
    settings = Settings()
    path = settings.data_root.resolve() / "rna_companion.db"
    if not path.is_file():
        raise ValueError("正式SQLite数据库不存在，拒绝创建空库替代")
    # 重建索引不启动worker、模型服务或reranker。
    runtime = MemoryStorageRuntime(
        settings.model_copy(update={"memory_enabled": True, "memory_production_enabled": False}),
        database_path=path,
    )
    try:
        await runtime.start(initialize_database=False)
        count = await reindex_character(runtime.database, runtime.get_index_repository(), character_id)
        print(f"Reindexed {count} published documents for {character_id}")
    finally:
        await runtime.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--character-id", required=True)
    asyncio.run(main(parser.parse_args().character_id))
