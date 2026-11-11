from __future__ import annotations
import asyncio
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from module.memory_recall.retrieval.core.domain.retrieval import RecallHit
from qdrant_client import models
from storage.embedding.bge import BgeMemoryEncoder
from storage.qdrant.memory_schema import COLLECTION


class QdrantMemorySearchRepository:
    def __init__(self, client, encoder) -> None:
        self._client, self._encoder = client, encoder

    async def search(self, query: str, *, character_id: str,
                     refs: tuple[MemoryRevisionRef, ...], limit: int) -> tuple[RecallHit, ...]:
        if not refs or not await self._client.collection_exists(COLLECTION):
            return ()
        if self._encoder is None:
            raise ValueError("语义召回缺少BGE-M3编码器")
        # 限定正式版本后再检索，未激活或旧版本分区不能占用当前文档的命中证据。
        allowed = {ref for ref in refs}
        versions = [models.Filter(must=[
            models.FieldCondition(key="document_id", match=models.MatchValue(value=ref.memory_id)),
            models.FieldCondition(key="kind", match=models.MatchValue(value=ref.kind.value)),
            models.FieldCondition(key="revision", match=models.MatchValue(value=ref.revision)),
            models.FieldCondition(key="content_hash", match=models.MatchValue(value=ref.content_hash)),
        ]) for ref in refs]
        qfilter = models.Filter(must=[
            models.FieldCondition(key="character_id", match=models.MatchValue(value=character_id)),
            models.FieldCondition(key="pool", match=models.MatchValue(value="hot")),
            models.Filter(should=versions),
        ])
        vectors = (await asyncio.to_thread(BgeMemoryEncoder.encode_dual_vectors,
                                           [query], self._encoder))[0]
        # daily最多五个point，扩大分区候选后按文档聚合，防止同篇分区挤占名额。
        fetch = max(limit * 5, 1)
        response = await self._client.query_points(COLLECTION, prefetch=[
            models.Prefetch(query=vectors["dense"], using="dense", limit=fetch, filter=qfilter),
            models.Prefetch(query=models.SparseVector(**vectors["sparse"]), using="sparse", limit=fetch, filter=qfilter),
        ], query=models.RrfQuery(rrf=models.Rrf(weights=[3.0, 1.0], k=60)),
            query_filter=qfilter, limit=fetch, with_payload=True)
        result, seen = [], set()
        for point in response.points:
            payload = point.payload or {}
            ref = MemoryRevisionRef(MemoryKind(payload["kind"]), payload["document_id"],
                payload["revision"], payload["content_hash"])
            if ref in allowed and ref not in seen and payload.get("character_id") == character_id:
                result.append(RecallHit(ref, point.score))
                seen.add(ref)
        return tuple(result[:limit])
