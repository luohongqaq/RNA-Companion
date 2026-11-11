from __future__ import annotations
from collections.abc import Callable
from module.shared.common.errors.base import InvalidStateError
from dataclasses import replace
from module.memory_management.object.core.domain.document import MemoryObject, ObjectState
from module.memory_management.topic.core.domain.ports import TopicProductionRepository
from module.memory_management.topic.core.domain.retrieval import (
    LinkedTopicRecall,
    ObjectRecallHit,
    ObjectRerankPair,
    RelatedObjectRecall,
    RelatedTopicRecall,
    TopicRecallHit,
    TopicRerankPair,
)
from module.memory_management.topic.core.processing.config import ObjectRecallConfig, TopicRecallConfig
from module.memory_management.topic.core.processing.ports import RelatedMemoryRerankPort, RelatedMemorySearchPort


class RelatedMemoryRetrieval:
    def __init__(self, repository: TopicProductionRepository, *,
                 recall_repository: RelatedMemorySearchPort | None = None,
                 rerank_repository: RelatedMemoryRerankPort | None = None,
                 recall_config: ObjectRecallConfig | None = None,
                 topic_recall_config: TopicRecallConfig | None = None) -> None:
        self._repository = repository
        self._recall_repository = recall_repository
        self._rerank_repository = rerank_repository
        self._recall_config = ObjectRecallConfig() if recall_config is None else recall_config
        self._topic_recall_config = TopicRecallConfig() if topic_recall_config is None else topic_recall_config

    async def recall_related_objects(self, objects: tuple[MemoryObject, ...]) -> tuple[RelatedObjectRecall, ...]:
        """仅召回STAGED object，核对SQLite后作为待整合成员。
        名称、内容分区检索 → RRF合并
            → SQLite核对并读取正式object(分区去重，只留下STAGED object)
            → reranker批量打分
            → 相关性门槛过滤
            → 断崖检测
            → 最多保留config.limit个
        """
        if not objects:
            return ()
        if self._recall_repository is None:
            raise NotImplementedError("topic生产侧object召回尚未接入")
        character_id = objects[0].character_id
        if any(item.character_id != character_id for item in objects):
            raise InvalidStateError("同一批object召回不能跨角色")
        config = self._recall_config
        # 只召回一次；过滤后不足limit也直接返回，不自动扩大查询。
        # 每个新 object 在 Qdrant 做稀疏+稠密混合检索（RRF 融合 + 截断）
        groups = await self._recall_repository.batch_object_search(objects, config=config)
        # 对命中的object_id去重，并保持顺序
        ids = tuple(dict.fromkeys(hit.object_id for group in groups for hit in group.hits))
        # 按命中ID读取SQLite正式object，核对当前状态。
        formal = {item.object_id: item for item in await self._repository.read_objects(ids, character_id=character_id)}
        sources = {item.object_id: item for item in objects}
        valid_groups = []
        pairs = []
        # 核对候选并去重
        for group in groups:
            hits = tuple(hit for hit in group.hits if hit.object_id in formal
                         and formal[hit.object_id].state == ObjectState.STAGED
                         and formal[hit.object_id].recall_tier in config.pools)
            valid_groups.append((group, hits))
            # 按实际命中的分区分别打分；同一候选可能生成名称、正文两对输入。
            for hit in hits:
                source, target = sources[group.source_object_id], formal[hit.object_id]
                if "name" in hit.paths:
                    pairs.append(ObjectRerankPair(group.source_object_id, hit.object_id,
                        source.object, target.object, "name"))
                if "content" in hit.paths:
                    pairs.append(ObjectRerankPair(group.source_object_id, hit.object_id,
                        source.index_text(), target.index_text(), "content"))
        scores = {}
        if pairs:
            if self._rerank_repository is None:
                raise InvalidStateError("object召回缺少reranker")
            scores = {(item.source_object_id, item.object_id, item.section_key): item.score
                      for item in await self._rerank_repository.batch_object_rerank(tuple(pairs))}
            if set(scores) != {(pair.source_object_id, pair.object_id, pair.section_key) for pair in pairs}:
                raise InvalidStateError("reranker结果未完整对应输入候选")
        results = []
        for group, valid_hits in valid_groups:
            ranked = []
            for hit in valid_hits:
                #名称、正文现在分别使用不同指令打分，结果会保留两份分数，并取较高分参与最终筛选。
                name_score = scores.get((group.source_object_id, hit.object_id, "name"))
                content_score = scores.get((group.source_object_id, hit.object_id, "content"))
                ranked.append(replace(hit, name_rerank_score=name_score, content_rerank_score=content_score,
                    rerank_score=max(score for score in (name_score, content_score) if score is not None)))
            # 断崖检测选topk
            hits = self._select_reranked_hits(ranked, config, tie_breaker=lambda hit: (hit.rank, hit.object_id))
            results.append(RelatedObjectRecall(group.source_object_id,
                tuple(formal[hit.object_id] for hit in hits), hits, group.truncated))
        return tuple(results)

    @staticmethod
    def _select_reranked_hits[Hit: ObjectRecallHit | TopicRecallHit](
        hits: list[Hit], config: ObjectRecallConfig, *,
        tie_breaker: Callable[[Hit], tuple] | None = None,
    ) -> tuple[Hit, ...]:
        """重排取前limit、地板过滤、只切首个断崖，最后生成连续排名。"""
        ranked = sorted(hits, key=lambda hit: (
            -hit.rerank_score, *(tie_breaker(hit) if tie_breaker is not None else ())))[:config.limit]
        selected = [hit for hit in ranked if hit.rerank_score >= config.min_rerank_score]
        for index in range(len(selected) - 1):
            current_score = selected[index].rerank_score
            next_score = selected[index + 1].rerank_score
            # 等价于next/current <= ratio；乘法避免除零，护栏采用严格小于。
            if next_score <= current_score * config.cliff_ratio and next_score < config.cliff_score_guard:
                selected = selected[:index + 1]
                break
        return tuple(replace(hit, rank=rank) for rank, hit in enumerate(selected, 1))

    async def recall_linked_topics(self, objects: tuple[MemoryObject, ...]) -> tuple[LinkedTopicRecall, ...]:
        """独立检索LINKED导航线索；只返回topic ID，不混入待整合object。"""
        if not objects:
            return ()
        if self._recall_repository is None:
            raise NotImplementedError("topic导航召回尚未接入")
        character_id = objects[0].character_id
        if any(item.character_id != character_id for item in objects):
            raise InvalidStateError("同一批topic导航不能跨角色")
        config = self._recall_config
        groups = await self._recall_repository.batch_linked_topic_search(objects, config=config)
        ids = tuple(dict.fromkeys(hit.object_id for group in groups for hit in group.hits))
        formal = {item.object_id: item for item in
                  await self._repository.read_objects(ids, character_id=character_id)}
        results = []
        for group in groups:
            topics, hits = [], []
            for hit in group.hits:
                item = formal.get(hit.object_id)
                if (item is None or item.recall_tier not in config.pools
                        or item.state != ObjectState.LINKED or not item.linked_topic_id
                        or item.linked_topic_id in topics):
                    continue
                topics.append(item.linked_topic_id)
                hits.append(hit)
            results.append(LinkedTopicRecall(group.source_object_id, tuple(topics), tuple(hits), group.truncated))
        return tuple(results)

    async def recall_related_topics(self, objects: tuple[MemoryObject, ...]) -> tuple[RelatedTopicRecall, ...]:
        """三分区检索＋LINKED导航，按每个新object-topic去重后仅做一次rerank。"""
        if not objects:
            return ()
        if self._recall_repository is None:
            raise InvalidStateError("topic召回缺少检索仓储")
        character_id = objects[0].character_id
        if any(item.character_id != character_id for item in objects):
            raise InvalidStateError("同一批topic召回不能跨角色")
        config = self._topic_recall_config
        # 直接召回和间接召回分别进行
        direct_groups = await self._recall_repository.batch_topic_search(objects, config=config)
        linked_groups = await self.recall_linked_topics(objects)
        direct_by_source = {group.source_object_id: group for group in direct_groups}
        linked_by_source = {group.source_object_id: group for group in linked_groups}
        # 按topic ID去重
        topic_ids = tuple(dict.fromkeys(
            [hit.topic_id for group in direct_groups for hit in group.hits]
            + [topic_id for group in linked_groups for topic_id in group.linked_topic_ids]))
        # 读正式原文
        formal = {item.ref.memory_id: item for item in await self._repository.read_topics(
            topic_ids, character_id=character_id, pools=config.pools)}
        # 组装rerank输入
        pairs, pending = [], []
        for source in objects:
            direct = direct_by_source.get(source.object_id)
            linked = linked_by_source.get(source.object_id)
            metadata = {}
            if direct is not None:
                for hit in direct.hits:
                    context = formal.get(hit.topic_id)
                    # 旧版本point不作为直接检索证据；不让其内容进入rerank。
                    if context is None or hit.revision != context.ref.revision:
                        continue
                    # 按 topic_id 的证据聚合器：同一个 topic 命中多个分区时，sections 收集它命中的分区名（去重），
                    # rrf_score 保留最高的粗排分（or 0 是为了处理首次的 None）。
                    info = metadata.setdefault(hit.topic_id, dict(sections=[], rrf_score=None, linked=False))
                    if hit.section_key not in info["sections"]:
                        info["sections"].append(hit.section_key)
                    info["rrf_score"] = max(info["rrf_score"] or 0, hit.score)
            # 注意 setdefault：如果一个 topic 既是直接检索命中又是导航目标，不会新建条目，只在原条目上补 linked 标志——去重合并
            navigation_ids = (() if linked is None else linked.linked_topic_ids)
            for topic_id in navigation_ids:
                if topic_id in formal:
                    # 把导航 topic并入 metadata，打上 linked = True 标记。
                    metadata.setdefault(topic_id, dict(sections=[], rrf_score=None, linked=False))["linked"] = True
            # 同一topic命中多个分区/导航只组装一对；不同新object仍分别比较。
            for topic_id in metadata:
                document = formal[topic_id].document
                text = (f"名称：{document.topic}\n事件主题描述：{document.topic_identity.description}"
                        f"\n当前状态：{document.current_state.text}")
                pairs.append(TopicRerankPair(source.object_id, topic_id, source.index_text(), text))
            pending.append((source.object_id, metadata,
                bool(direct and direct.truncated) or bool(linked and linked.truncated)))
        scores = {}
        if pairs:
            if self._rerank_repository is None:
                raise InvalidStateError("topic召回缺少reranker")
            scores = {(item.source_object_id, item.topic_id): item.score
                      for item in await self._rerank_repository.batch_topic_rerank(tuple(pairs))}
            if set(scores) != {(pair.source_object_id, pair.topic_id) for pair in pairs}:
                raise InvalidStateError("topic重排结果未完整对应输入候选")

        results = []
        for source_id, metadata, truncated in pending:
            hits = [TopicRecallHit(source_id, topic_id, 0, info["rrf_score"], scores[(source_id, topic_id)],
                    tuple(info["sections"]), info["linked"]) for topic_id, info in metadata.items()
                    ]
            selected = self._select_reranked_hits(hits, config,
                tie_breaker=lambda hit: (-(hit.rrf_score or 0), hit.topic_id))
            results.append(RelatedTopicRecall(source_id, tuple(formal[hit.topic_id] for hit in selected),
                selected, truncated))
        return tuple(results)
