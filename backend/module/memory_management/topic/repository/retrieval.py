from __future__ import annotations
import asyncio
from module.shared.memory.types.encoding import DualEncoder
from module.shared.memory.types.indexing import IndexHit
from module.memory_management.shared.indexing.repository.qdrant import QdrantMemoryIndexRepository
from module.memory_management.object.core.domain.document import MemoryObject, ObjectState
from module.memory_management.shared.contracts.errors import MemoryIndexUnavailable
from module.memory_management.topic.core.domain.retrieval import (
    ObjectRecallGroup,
    ObjectRecallHit,
    TopicSearchGroup,
    TopicSearchHit,
)
from module.memory_management.topic.core.processing.config import ObjectRecallConfig, TopicRecallConfig
from qdrant_client import AsyncQdrantClient, models
from storage.qdrant.memory_schema import COLLECTION


class QdrantMemoryRecallRepository:
    """新分区混合检索；编码器由外层与索引持久层共享。"""
    def __init__(self, client: AsyncQdrantClient, encoder: DualEncoder, *,
                 index_repository: QdrantMemoryIndexRepository | None = None) -> None:
        self._client, self._encoder = client, encoder
        self._query_encoder = index_repository if index_repository is not None else QdrantMemoryIndexRepository(client, encoder)

    async def hybrid_search(self, query: str, *, character_id: str, limit: int = 10) -> tuple[IndexHit, ...]:
        """同编码器查询，dense+sparse融合；只返回候选，正式版本由业务层核验。"""
        if not query.strip() or not character_id or limit < 1:
            raise ValueError("检索需要文本、角色和正数limit")
        try:
            if not await self._client.collection_exists(COLLECTION):
                return ()
            vector = (await asyncio.to_thread(QdrantMemoryIndexRepository.encode_dual_vectors, [query], self._encoder))[0]
            filters = models.Filter(must=[
                models.FieldCondition(key="character_id", match=models.MatchValue(value=character_id)),
                models.FieldCondition(key="kind", match=models.MatchValue(value="daily"))],
                must_not=[models.IsEmptyCondition(is_empty=models.PayloadField(key="memory_date"))])
            result = await self._client.query_points(COLLECTION,
                prefetch=[models.Prefetch(query=vector["dense"], using="dense", filter=filters, limit=limit*3),
                          models.Prefetch(query=models.SparseVector(**vector["sparse"]), using="sparse", filter=filters, limit=limit*3)],
                query=models.FusionQuery(fusion=models.Fusion.RRF), query_filter=filters,
                limit=limit, with_payload=True)
            hits = []
            for point in result.points:
                data = point.payload
                fields = ("memorable_moments", "profile_facts", "intentions", "commitments", "reflections")
                if not all(key in data for key in fields):
                    continue
                hits.append(IndexHit(str(point.id), data["character_id"], point.score,
                    data["title"], data["memory_date"], {key: data[key] for key in fields}))
            return tuple(hits)
        except Exception as error:
            raise MemoryIndexUnavailable(f"混合检索未完成（{type(error).__name__}）",
                retryable=not isinstance(error, (ValueError, TypeError))) from error


    @staticmethod
    def _weighted_rrf(rankings: list[list[ObjectRecallHit]], *, weights: list[float],
                      rrf_k: int = 60) -> list[ObjectRecallHit]:
        """输入同一新object的多路已排名对象，按显式rank融合，返回最终有序列表。"""
        scores, objects, paths = {}, {}, {}
        for ranked_objects, weight in zip(rankings, weights, strict=True):
            if weight <= 0:
                continue
            # 每路已按object_id去重，直接读取对象已有的rank。
            for item in ranked_objects:
                object_id = item.object_id
                scores[object_id] = scores.get(object_id, 0.0) + weight / (rrf_k + item.rank)
                objects[object_id] = item
                paths.setdefault(object_id, []).extend(item.paths)

        ordered_ids = sorted(scores, key=lambda cid: (-scores[cid], cid))
        return [ObjectRecallHit(objects[cid].source_object_id, cid, final_rank,
                    scores[cid], tuple(dict.fromkeys(paths[cid])))
                for final_rank, cid in enumerate(ordered_ids, start=1)]

    async def batch_object_search(self, objects: tuple[MemoryObject, ...], *,
                                  config: ObjectRecallConfig) -> tuple[ObjectRecallGroup, ...]:
        """独立召回STAGED object：名称/内容分区混合检索，只用于待整合成员。"""
        if not objects:
            return ()
        character_id = objects[0].character_id
        if any(item.character_id != character_id for item in objects):
            raise ValueError("同一批object召回不能跨角色")
        try:
            collection = QdrantMemoryIndexRepository.object_collection_name
            if not await self._client.collection_exists(collection):
                return tuple(ObjectRecallGroup(item.object_id, ()) for item in objects)
            sections = [section for item in objects
                        for section in QdrantMemoryIndexRepository._object_sections(item)]
            # CPU密集型工作，扔去线性池，避免阻塞时间循环
            vectors = await asyncio.to_thread(self._query_encoder._encode, [text for _, text in sections])
            filters = models.Filter(must=[
                models.FieldCondition(key="character_id", match=models.MatchValue(value=character_id)),
                models.FieldCondition(key="kind", match=models.MatchValue(value="object")),
                models.FieldCondition(key="pool", match=models.MatchAny(any=[pool.value for pool in config.pools])),
            ], must_not=[models.FieldCondition(key="object_id", match=models.MatchAny(
                any=[item.object_id for item in objects]))])
            requests = []
            for i in range(len(objects)):
                for (section_key, _), vector in zip(sections[i*2:i*2+2], vectors[i*2:i*2+2], strict=True):
                    # 名称只查名称分区，完整内容只查内容分区；旧的无分区point不会参与召回。
                    section_filter = models.Filter(
                        must=[*filters.must,
                            models.FieldCondition(key="state", match=models.MatchValue(value=ObjectState.STAGED.value)),
                            models.FieldCondition(key="section_key", match=models.MatchValue(value=section_key))],
                        must_not=filters.must_not,
                    )
                    requests.extend((
                        models.QueryRequest(query=vector["dense"], using="dense", filter=section_filter,
                                            limit=config.fetch_limit, with_payload=True),
                        models.QueryRequest(query=vector["sparse"], using="sparse", filter=section_filter,
                                            limit=config.fetch_limit, with_payload=True),
                    ))
            responses = iter(await self._client.query_batch_points(collection, requests=requests))
            groups = []
            for memory_object in objects:
                merged_hits = {}
                truncated = False
                # 请求与结果均按每个object的名称、内容排列，每个分区先dense再sparse。
                for path in ("name", "content"):
                    dense_result = next(responses)
                    sparse_result = next(responses)
                    dense_objects = [ObjectRecallHit(memory_object.object_id,
                        point.payload["object_id"], rank, point.score, (path,))
                        for rank, point in enumerate(dense_result.points, start=1)]
                    sparse_objects = [ObjectRecallHit(memory_object.object_id,
                        point.payload["object_id"], rank, point.score, (path,))
                        for rank, point in enumerate(sparse_result.points, start=1) if point.score > 0]

                    # 两路带排名的object直接交给RRF，得到分区内排好序的列表。
                    sorted_objects = self._weighted_rrf(
                        [dense_objects, sparse_objects],
                        weights=[config.dense_weight, config.sparse_weight], rrf_k=config.rrf_k,
                    )
                    # 合并去重后加入最终列表（重复object取max分）
                    for item in sorted_objects:
                        previous = merged_hits.get(item.object_id)
                        if previous is None:
                            merged_hits[item.object_id] = item
                        else:
                            merged_hits[item.object_id] = ObjectRecallHit(
                                memory_object.object_id, item.object_id, 0,
                                max(previous.score, item.score), previous.paths + item.paths)
                    truncated |= (len(dense_result.points) >= config.fetch_limit
                                  or len(sparse_result.points) >= config.fetch_limit)

                # 名称、内容分区合并去重后取较高分，生成最终列表。
                ordered = sorted(merged_hits.values(), key=lambda item: (-item.score, item.object_id))[:config.rrf_limit]
                hits = tuple(ObjectRecallHit(item.source_object_id, item.object_id,
                             rank, item.score, item.paths) for rank, item in enumerate(ordered, start=1))
                groups.append(ObjectRecallGroup(memory_object.object_id, hits, truncated))
            return tuple(groups)
        except Exception as error:
            raise MemoryIndexUnavailable(f"object混合召回未完成（{type(error).__name__}）",
                retryable=not isinstance(error, (ValueError, TypeError))) from error

    async def batch_linked_topic_search(self, objects: tuple[MemoryObject, ...], *,
                                  config: ObjectRecallConfig) -> tuple[ObjectRecallGroup, ...]:
        """独立召回LINKED导航线索：不经过object召回入口，后续由SQLite解析topic。"""
        if not objects:
            return ()
        character_id = objects[0].character_id
        if any(item.character_id != character_id for item in objects):
            raise ValueError("同一批topic导航不能跨角色")
        try:
            collection = QdrantMemoryIndexRepository.object_collection_name
            if not await self._client.collection_exists(collection):
                return tuple(ObjectRecallGroup(item.object_id, ()) for item in objects)
            sections = [section for item in objects
                        for section in QdrantMemoryIndexRepository._object_sections(item)]
            # CPU密集型工作，扔去线性池，避免阻塞时间循环
            vectors = await asyncio.to_thread(self._query_encoder._encode, [text for _, text in sections])
            filters = models.Filter(must=[
                models.FieldCondition(key="character_id", match=models.MatchValue(value=character_id)),
                models.FieldCondition(key="kind", match=models.MatchValue(value="object")),
                models.FieldCondition(key="pool", match=models.MatchAny(any=[pool.value for pool in config.pools])),
            ], must_not=[models.FieldCondition(key="object_id", match=models.MatchAny(
                any=[item.object_id for item in objects]))])
            requests = []
            for i in range(len(objects)):
                for (section_key, _), vector in zip(sections[i*2:i*2+2], vectors[i*2:i*2+2], strict=True):
                    # 名称只查名称分区，完整内容只查内容分区；旧的无分区point不会参与召回。
                    section_filter = models.Filter(
                        must=[*filters.must,
                            models.FieldCondition(key="state", match=models.MatchValue(value=ObjectState.LINKED.value)),
                            models.FieldCondition(key="section_key", match=models.MatchValue(value=section_key))],
                        must_not=filters.must_not,
                    )
                    requests.extend((
                        models.QueryRequest(query=vector["dense"], using="dense", filter=section_filter,
                                            limit=config.fetch_limit, with_payload=True),
                        models.QueryRequest(query=vector["sparse"], using="sparse", filter=section_filter,
                                            limit=config.fetch_limit, with_payload=True),
                    ))
            responses = iter(await self._client.query_batch_points(collection, requests=requests))
            groups = []
            for memory_object in objects:
                merged_hits = {}
                truncated = False
                # 请求与结果均按每个object的名称、内容排列，每个分区先dense再sparse。
                for path in ("name", "content"):
                    dense_result = next(responses)
                    sparse_result = next(responses)
                    dense_objects = [ObjectRecallHit(memory_object.object_id,
                        point.payload["object_id"], rank, point.score, (path,))
                        for rank, point in enumerate(dense_result.points, start=1)]
                    sparse_objects = [ObjectRecallHit(memory_object.object_id,
                        point.payload["object_id"], rank, point.score, (path,))
                        for rank, point in enumerate(sparse_result.points, start=1) if point.score > 0]

                    # 两路带排名的object直接交给RRF，得到分区内排好序的列表。
                    sorted_objects = self._weighted_rrf(
                        [dense_objects, sparse_objects],
                        weights=[config.dense_weight, config.sparse_weight], rrf_k=config.rrf_k,
                    )
                    for item in sorted_objects:
                        previous = merged_hits.get(item.object_id)
                        if previous is None:
                            merged_hits[item.object_id] = item
                        else:
                            merged_hits[item.object_id] = ObjectRecallHit(
                                memory_object.object_id, item.object_id, 0,
                                max(previous.score, item.score), previous.paths + item.paths)
                    truncated |= (len(dense_result.points) >= config.fetch_limit
                                  or len(sparse_result.points) >= config.fetch_limit)

                # 名称、内容分区合并去重后取较高分，生成最终列表。
                ordered = sorted(merged_hits.values(), key=lambda item: (-item.score, item.object_id))[:config.rrf_limit]
                hits = tuple(ObjectRecallHit(item.source_object_id, item.object_id,
                             rank, item.score, item.paths) for rank, item in enumerate(ordered, start=1))
                groups.append(ObjectRecallGroup(memory_object.object_id, hits, truncated))
            return tuple(groups)
        except Exception as error:
            raise MemoryIndexUnavailable(f"topic导航召回未完成（{type(error).__name__}）",
                retryable=not isinstance(error, (ValueError, TypeError))) from error


    async def batch_topic_search(self, objects: tuple[MemoryObject, ...], *,
                                 config: TopicRecallConfig) -> tuple[TopicSearchGroup, ...]:
        """三分区独立混合检索；不查时间线，不在读取正式正文前截取最终limit。"""
        if not objects:
            return ()
        character_id = objects[0].character_id
        if any(item.character_id != character_id for item in objects):
            raise ValueError("同一批topic召回不能跨角色")
        try:
            collection = QdrantMemoryIndexRepository.collection_name
            if not await self._client.collection_exists(collection):
                return tuple(TopicSearchGroup(item.object_id, ()) for item in objects)
            # 身份、当前状态共用新object全文向量；每个object只编码名称、全文两段。
            texts = [text for item in objects for text in (item.object, item.index_text())]
            vectors = await asyncio.to_thread(self._query_encoder._encode, texts)
            requests = []
            sections = ("topic_name", "topic_identity", "topic_current")
            for index, memory_object in enumerate(objects):
                name_vector, content_vector = vectors[index*2:index*2+2]
                for section, vector in zip(sections, (name_vector, content_vector, content_vector), strict=True):
                    filters = models.Filter(must=[
                        models.FieldCondition(key="character_id", match=models.MatchValue(value=character_id)),
                        models.FieldCondition(key="kind", match=models.MatchValue(value="topic")),
                        models.FieldCondition(key="pool", match=models.MatchAny(any=[pool.value for pool in config.pools])),
                        models.FieldCondition(key="section_key", match=models.MatchValue(value=section)),
                    ])
                    requests.extend((
                        models.QueryRequest(query=vector["dense"], using="dense", filter=filters,
                                            limit=config.fetch_limit, with_payload=True),
                        models.QueryRequest(query=vector["sparse"], using="sparse", filter=filters,
                                            limit=config.fetch_limit, with_payload=True),
                    ))
            responses = iter(await self._client.query_batch_points(collection, requests=requests))
            groups = []
            for memory_object in objects:
                hits, truncated = [], False
                for section in sections:
                    dense_result, sparse_result = next(responses), next(responses)
                    # 用point身份融合，保留同一topic不同版本的命中，交由SQLite版本核对。
                    by_point = {str(point.id): point for result in (dense_result, sparse_result)
                                for point in result.points if (point.payload or {}).get("topic_id")
                                and type((point.payload or {}).get("revision")) is int}
                    dense_objects = [ObjectRecallHit(memory_object.object_id, str(point.id), rank,
                        point.score, (section,)) for rank, point in enumerate(dense_result.points, 1)
                        if str(point.id) in by_point]
                    sparse_objects = [ObjectRecallHit(memory_object.object_id, str(point.id), rank,
                        point.score, (section,)) for rank, point in enumerate(sparse_result.points, 1)
                        if str(point.id) in by_point and point.score > 0]
                    ranked = self._weighted_rrf([dense_objects, sparse_objects],
                        weights=[config.dense_weight, config.sparse_weight], rrf_k=config.rrf_k)
                    for hit in ranked:
                        point = by_point[hit.object_id]
                        hits.append(TopicSearchHit(memory_object.object_id, point.payload["topic_id"],
                            point.payload["revision"], section, str(point.id), hit.rank, hit.score))
                    truncated |= (len(dense_result.points) >= config.fetch_limit
                                  or len(sparse_result.points) >= config.fetch_limit)
                hits.sort(key=lambda hit: (-hit.score, hit.topic_id, hit.section_key, hit.point_id))
                # 同一topic多个分区只占一个粗排名额，保留入选topic的所有分区证据。
                topic_ids = tuple(dict.fromkeys(hit.topic_id for hit in hits))[:config.rrf_limit]
                selected_ids = set(topic_ids)
                groups.append(TopicSearchGroup(memory_object.object_id,
                    tuple(hit for hit in hits if hit.topic_id in selected_ids), truncated))
            return tuple(groups)
        except Exception as error:
            raise MemoryIndexUnavailable(f"topic混合召回未完成（{type(error).__name__}）",
                retryable=not isinstance(error, (ValueError, TypeError))) from error
