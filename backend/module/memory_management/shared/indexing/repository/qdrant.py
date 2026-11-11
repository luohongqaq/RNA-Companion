from __future__ import annotations
from module.shared.common.time import BackendTime
from storage.embedding.bge import BgeMemoryEncoder
import asyncio
import hashlib
import json
import uuid
from module.shared.memory.types.encoding import DualEncoder
from module.shared.memory.types.references import MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier
from module.memory_management.shared.core.domain.document import VectorDocument
from dataclasses import asdict
from module.memory_management.shared.indexing.core.domain.representation import IndexRepresentation
from module.memory_management.object.core.domain.document import MemoryObject
from module.memory_management.shared.contracts.errors import MemoryIndexUnavailable
from module.memory_management.shared.indexing.core.domain.ports import SeedPublicationPort
from pathlib import Path
from qdrant_client import AsyncQdrantClient, models
from storage.qdrant.memory_schema import COLLECTION, DIM, _POINT_NAMESPACE
from threading import Lock
from typing import Self
from uuid import NAMESPACE_URL, uuid5


class QdrantMemoryIndexRepository(BgeMemoryEncoder):
    """共用编码与索引实现；正文集合和 object 专用集合分别写入。"""

    collection_name = COLLECTION
    object_collection_name = "memory_objects"

    def __init__(self, client: AsyncQdrantClient, encoder, *,
                 model_version: str = "bge-m3-local", batch_size: int = 4,
                 max_length: int = 8192) -> None:
        self._client, self._encoder = client, encoder
        self._model_version, self._batch_size, self._max_length = model_version, batch_size, max_length
        self._encode_lock = Lock()
        self._collection_lock = asyncio.Lock()

    @classmethod
    def from_shared(cls, repository: QdrantMemoryIndexRepository) -> Self:
        """派生功能仓储借用连接、编码器及锁，不重复加载或持有资源生命周期。"""
        instance = cls(repository._client, repository._encoder,
            model_version=repository._model_version, batch_size=repository._batch_size,
            max_length=repository._max_length)
        instance._encode_lock = repository._encode_lock
        instance._collection_lock = repository._collection_lock
        return instance

    @classmethod
    async def load(cls, client: AsyncQdrantClient, model_path: Path, **kwargs):
        """应用初始化时加载本地模型一次；client由外层负责关闭。"""
        encoder = await asyncio.to_thread(QdrantMemoryIndexRepository.load_encoder, str(model_path))
        return cls(client, encoder, **kwargs)

    def _encode(self, texts: list[str]) -> list[dict]:
        with self._encode_lock:
            tokenizer = getattr(self._encoder, "tokenizer", None)
            if tokenizer is not None:
                lengths = tokenizer(texts, truncation=False, padding=False)["input_ids"]
                if any(len(tokens) > self._max_length for tokens in lengths):
                    raise ValueError("分区超过编码器输入容量，拒绝静默截断")
            output = self._encoder.encode(texts, batch_size=self._batch_size,
                max_length=self._max_length, return_dense=True, return_sparse=True,
                return_colbert_vecs=False)
            return [{"dense": dense.tolist(), "sparse": models.SparseVector(
                indices=[int(index) for index in sparse], values=[float(value) for value in sparse.values()])}
                for dense, sparse in zip(output["dense_vecs"], output["lexical_weights"], strict=True)]

    async def _ensure_collection(self, collection_name: str | None = None):
        collection_name = self.collection_name if collection_name is None else collection_name
        async with self._collection_lock:
            if not await self._client.collection_exists(collection_name):
                await self._client.create_collection(collection_name,
                    vectors_config={"dense": models.VectorParams(size=DIM, distance=models.Distance.COSINE)},
                    sparse_vectors_config={"sparse": models.SparseVectorParams()})

    async def initialize(self) -> None:
        """应用启动时准备集合（正文集合与 object 集合）；不写入任何记忆。"""
        await self._ensure_collection()
        await self._ensure_collection(self.object_collection_name)

    def _point_id(self, item: IndexRepresentation) -> str:
        identity = [item.character_id, item.ref.kind.value, item.ref.memory_id,
                    item.ref.revision, item.ref.content_hash, item.section_key, self._model_version]
        return str(uuid5(NAMESPACE_URL, json.dumps(identity, ensure_ascii=False)))

    async def set_memory_pool(self, character_id: str, ref: MemoryRevisionRef, tier: RecallTier) -> bool:
        """更新当前正式版本的全部分区标签，返回是否找到索引。"""
        if not await self._client.collection_exists(self.collection_name):
            return False
        points = models.Filter(must=[
            models.FieldCondition(key=key, match=models.MatchValue(value=value))
            for key, value in (
                ("character_id", character_id),
                ("kind", ref.kind.value),
                ("document_id", ref.memory_id),
                ("revision", ref.revision),
                ("content_hash", ref.content_hash),
            )
        ])
        count = await self._client.count(self.collection_name, count_filter=points, exact=True)
        if count.count == 0:
            return False
        result = await self._client.set_payload(self.collection_name, {"pool": tier.value}, points, wait=True)
        if result.status != models.UpdateStatus.COMPLETED:
            raise MemoryIndexUnavailable("记忆池标签写入未确认")
        return True

    async def set_object_pool(self, character_id: str, object_id: str, tier: RecallTier) -> bool:
        """更新object的池标签；找不到point返回False，由调用方决定是否暂缓降池。"""
        if not await self._client.collection_exists(self.object_collection_name):
            return False
        points = models.Filter(must=[
            models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
            models.FieldCondition(key="character_id", match=models.MatchValue(value=character_id)),
            models.FieldCondition(key="kind", match=models.MatchValue(value="object")),
        ])
        count = await self._client.count(self.object_collection_name, count_filter=points, exact=True)
        if count.count == 0:
            return False
        result = await self._client.set_payload(
            self.object_collection_name, {"pool": tier.value}, points, wait=True)
        if result.status != models.UpdateStatus.COMPLETED:
            raise MemoryIndexUnavailable("object池标签写入未确认")
        return True

    async def batch_hybrid_index(self, document: VectorDocument,
                                 representations: tuple[IndexRepresentation, ...]) -> bool:
        """一批分区编码dense+sparse并确认写入；重试相同ID覆盖，不碰SQLite。"""
        if any(item.ref != document.ref or item.character_id != document.character_id for item in representations):
            raise ValueError("索引分区与文档归属不一致")
        if len({item.section_key for item in representations}) != len(representations):
            raise ValueError("同一文档的分区索引不能重复")
        try:
            await self._ensure_collection()
            if not representations:
                return True
            vectors = await asyncio.to_thread(self._encode, [item.text for item in representations])
            points = []
            for item, vector in zip(representations, vectors, strict=True):
                payload = {**document.document.public_payload(),
                    "character_id": document.character_id, "kind": document.ref.kind.value,
                    "pool": item.recall_tier.value,
                    "document_id": document.ref.memory_id, "revision": document.ref.revision,
                    "content_hash": document.ref.content_hash, "section_key": item.section_key,
                    "vector_document_key": document.vector_document_key}
                if document.ref.kind.value == "topic":
                    # 三个point保留完整公开正文，通过section_key限定具体检索分区。
                    payload["section_key"] = item.section_key
                    payload["topic_id"] = document.ref.memory_id
                    payload["revision"] = document.ref.revision
                points.append(models.PointStruct(id=self._point_id(item), payload=payload, vector=vector))
            response = await self._client.upsert(self.collection_name, points=points, wait=True)
            return response.status == models.UpdateStatus.COMPLETED
        except Exception as error:
            raise MemoryIndexUnavailable(f"混合索引未完成（{type(error).__name__}）",
                retryable=not isinstance(error, (ValueError, TypeError))) from error

    @staticmethod
    def _object_payload(memory_object: MemoryObject) -> dict:
        """保留完整事件、计划及来源，供后续object检索与topic整合使用。"""
        return json.loads(json.dumps({
            "object_id": memory_object.object_id, "character_id": memory_object.character_id,
            "kind": "object", "object": memory_object.object,
            "events": [asdict(event) for event in memory_object.events],
            "plans": [asdict(plan) for plan in memory_object.plans],
            "forgetfulness": memory_object.forgetfulness, "pool": memory_object.recall_tier.value,
            "state": memory_object.state.value, "linked_topic_id": memory_object.linked_topic_id,
            "created_at": memory_object.created_at,
        }, ensure_ascii=False, default=str))

    @staticmethod
    def _object_sections(memory_object: MemoryObject) -> tuple[tuple[str, str], ...]:
        """同一object的两个检索表示，以object_id关联，不以名称关联。"""
        return (("object_name", memory_object.object), ("object_content", memory_object.index_text()))

    @staticmethod
    def _object_point_id(object_id: str, section_key: str) -> str:
        return str(uuid5(_POINT_NAMESPACE, json.dumps([object_id, section_key], ensure_ascii=False)))

    async def batch_object_index(self, objects: tuple[MemoryObject, ...]) -> bool:
        """每个object写名称、完整内容两个分区；整批确认后才返回成功。"""
        if not objects:
            return True
        try:
            if len({memory_object.object_id for memory_object in objects}) != len(objects):
                raise ValueError("object候选ID重复")
            await self._ensure_collection(self.object_collection_name)
            sections = [(memory_object, key, text) for memory_object in objects
                        for key, text in self._object_sections(memory_object)]
            vectors = await asyncio.to_thread(self._encode, [text for _, _, text in sections])
            points = [models.PointStruct(
                id=self._object_point_id(memory_object.object_id, key),
                payload={**self._object_payload(memory_object), "section_key": key}, vector=vector,
            ) for (memory_object, key, _), vector in zip(sections, vectors, strict=True)]
            response = await self._client.upsert(self.object_collection_name, points=points, wait=True)
            return response.status == models.UpdateStatus.COMPLETED
        except Exception as error:
            raise MemoryIndexUnavailable(f"object混合索引未完成（{type(error).__name__}）",
                retryable=not isinstance(error, (ValueError, TypeError))) from error

    async def update_object_metadata(self, objects: tuple[MemoryObject, ...]) -> bool:
        """更新已存在point的可变元数据；部分完成后重试可覆盖，不生成向量或新point。"""
        if not objects:
            return True
        try:
            for item in objects:
                response = await self._client.set_payload(self.object_collection_name,
                    payload={"state": item.state.value, "pool": item.recall_tier.value,
                             "linked_topic_id": item.linked_topic_id, "forgetfulness": item.forgetfulness},
                    points=models.Filter(must=[
                        models.FieldCondition(key="object_id", match=models.MatchValue(value=item.object_id)),
                        models.FieldCondition(key="character_id", match=models.MatchValue(value=item.character_id)),
                        models.FieldCondition(key="kind", match=models.MatchValue(value="object")),
                    ]), wait=True)
                if response.status != models.UpdateStatus.COMPLETED:
                    return False
            return True
        except Exception as error:
            raise MemoryIndexUnavailable(f"object元数据同步未完成（{type(error).__name__}）",
                retryable=not isinstance(error, (ValueError, TypeError))) from error

    @staticmethod
    def point_id(document_id: str) -> str:
        """旧格式point的确定性ID工具；新分区索引使用_point_id，正式正文从SQLite读取。"""
        return str(uuid.uuid5(_POINT_NAMESPACE, document_id))



    @staticmethod
    async def import_documents(client: AsyncQdrantClient, repository: SeedPublicationPort, documents: list[dict],
                               *, character_id: str, namespace: str, 
                               encoder: DualEncoder | None = None) -> dict:
        """种子导入也使用正式结构化正文hash和分区索引，不再写旧整篇point。"""
        from datetime import datetime
        from module.memory_management.daily.core.domain.document import DailyDocument
        from module.memory_management.daily.core.domain.publication import DailyWrite
        from module.memory_management.topic.core.domain.publication import TopicWrite
        from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
        from module.shared.memory.types.retention import RecallTier
        from module.memory_management.shared.repository import document_mappers
        versions = await repository.current_versions([doc["document_id"] for doc in documents], character_id)
        staged, indexed = [], 0
        index = QdrantMemoryIndexRepository(client, encoder)
        for original in documents:
            doc = dict(original)
            identifier = doc["document_id"]
            if doc["kind"] == "daily":
                session_id = "seed_session_" + hashlib.sha256(identifier.encode()).hexdigest()[:24]
                heard = datetime.fromisoformat(doc["conversation_time"])
                memory_date = doc.get("memory_date") or BackendTime.local(heard).date().isoformat()
                structured = DailyDocument.from_data(dict(doc["sections"], title=doc["title"],
                    memory_date=memory_date, source_session_ids=[session_id]))
                write = DailyWrite.from_document(structured)
                doc["memory_date"] = memory_date
            elif doc["kind"] == "topic":
                structured = document_mappers._memory_topic_document_from_json(dict(doc["sections"], topic=doc["title"]))
                write = TopicWrite.from_document(structured)
            else:
                raise ValueError("种子只支持正式daily/topic正文")
            digest = structured.content_hash()
            old = versions.get(identifier)
            revision = old[0] + int(old[1] != digest) if old else 1
            ref = MemoryRevisionRef(MemoryKind(doc["kind"]), identifier, revision, digest)
            key = f"{identifier}:r{revision}:{digest}"
            doc.update(character_id=character_id, revision=revision, content_hash=digest,
                vector_document_key=key, document=structured.to_data(), namespace=namespace)
            # 同版本再次导入仍确认索引，以修复旧payload或上次中断；不增加业务版本。
            if encoder is None:
                raise ValueError("分区索引需要真实编码器")
            vector = VectorDocument(key, ref, character_id, structured, (), (), doc["title"])
            if not await index.batch_hybrid_index(vector, write.index_representations(
                    ref, character_id, RecallTier(doc.get("pool", "hot")))):
                raise MemoryIndexUnavailable("种子分区索引尚未完成")
            staged.append(doc)
            indexed += 1
        counts = await repository.publish_seed(staged, character_id, namespace)
        return {**counts, "indexed": indexed, "collection": COLLECTION}
