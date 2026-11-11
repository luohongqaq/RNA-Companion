"""真实本地Qdrant验证分区payload、混合索引幂等与角色隔离；编码器使用确定性替身。"""
from datetime import date
from dataclasses import replace
import numpy as np
import pytest
from qdrant_client import AsyncQdrantClient
from module.memory_management.daily.core.domain.publication import DailyWrite
from module.memory_management.daily.core.domain.document import DailyDocument, DailyMoment, MomentKind, DailyProfileFact, ProfileFactKind
from module.memory_management.shared.core.domain.document import VectorDocument
from module.shared.memory.types.references import MemoryRevisionRef, MemoryKind
from module.shared.memory.types.retention import RecallTier
from module.memory_management.shared.indexing.repository.qdrant import QdrantMemoryIndexRepository
from module.memory_management.topic.repository.retrieval import QdrantMemoryRecallRepository


class Encoder:
    def __init__(self):
        self.calls = 0
        self.texts = []
    def encode(self, texts, **kwargs):
        self.calls += 1
        self.texts.extend(texts)
        return {"dense_vecs": np.ones((len(texts), 1024), dtype=np.float32),
                "lexical_weights": [{"1": 1.0} for _ in texts]}


def material(character_id="c", revision=1):
    doc = DailyDocument(date(2026,10,31), ("s",), "日记标题",
        memorable_moments=(DailyMoment(MomentKind.MILESTONE,"用户有一个值得记住的进展",("t",)),),
        profile_facts=(DailyProfileFact(ProfileFactKind.BACKGROUND,"用户有学习经历",("t",),("不要索引这段内部证据",)),))
    write = DailyWrite.from_document(doc)
    ref = MemoryRevisionRef(MemoryKind.DAILY,"daily:"+character_id,revision,doc.content_hash())
    vector = VectorDocument("key:"+character_id,ref,character_id,doc,(),(),doc.title)
    return vector, write.index_representations(ref,character_id,RecallTier.HOT)


@pytest.mark.asyncio
async def test_real_qdrant_section_payload_and_retry_is_idempotent(tmp_path):
    client = AsyncQdrantClient(path=str(tmp_path/"qdrant"))
    encoder = Encoder()
    index = QdrantMemoryIndexRepository(client, encoder)
    try:
        document,sections = material()
        assert len(sections)==2
        assert await index.batch_hybrid_index(document,sections)
        assert await index.batch_hybrid_index(document,sections)
        points,_ = await client.scroll(index.collection_name,limit=20,with_payload=True,with_vectors=True)
        assert len(points)==2 and encoder.calls==2
        for point in points:
            data=point.payload
            assert data["title"]=="日记标题" and data["memory_date"]=="2026-10-31"
            assert data["character_id"] == "c" and data["kind"] == "daily"
            assert set(data) == {"title", "memory_date", "character_id", "kind", "pool",
                "document_id", "revision", "content_hash", "section_key", "vector_document_key",
                "memorable_moments", "profile_facts", "intentions", "commitments", "reflections"}
            assert "用户有学习经历" in data["profile_facts"]
            assert "用户有一个值得记住的进展" in data["memorable_moments"]
            assert data["intentions"] == data["commitments"] == data["reflections"] == ""
            assert "不要索引这段内部证据" not in str(data) and "来源：" not in str(data)
            assert "dense" in point.vector and "sparse" in point.vector
        assert all(not ("用户有学习经历" in text and "用户有一个值得记住的进展" in text) for text in encoder.texts)
        # New revisions coexist until SQLite switches the active version.
        newer,representations=material(revision=2)
        assert await index.batch_hybrid_index(newer,representations)
        assert (await client.count(index.collection_name)).count==4
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_hybrid_search_filters_character_and_returns_section_refs(tmp_path):
    client=AsyncQdrantClient(path=str(tmp_path/"qdrant"))
    encoder=Encoder()
    index=QdrantMemoryIndexRepository(client, encoder)
    recall=QdrantMemoryRecallRepository(client,encoder)
    try:
        for cid in ("c","other"):
            document,representations=material(cid)
            await index.batch_hybrid_index(document,representations)
        hits=await recall.hybrid_search("学习经历",character_id="c")
        assert len(hits)==2 and all(hit.character_id=="c" for hit in hits)
        assert all("用户有学习经历" in hit.full_content and "用户有一个值得记住的进展" in hit.full_content for hit in hits)
        assert all(set(hit.sections)=={"memorable_moments","profile_facts","intentions","commitments","reflections"} for hit in hits)
        assert not await recall.hybrid_search("学习经历",character_id="missing")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_object_index_full_payload_and_partial_write_retry(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from module.memory_management.object.core.domain.document import MemoryObject, ObjectEvent, ObjectPlan
    from module.shared.memory.types.references import EvidenceRef
    from module.memory_management.shared.contracts.errors import MemoryIndexUnavailable
    client = AsyncQdrantClient(path=str(tmp_path / "objects"))
    encoder = Encoder()
    index = QdrantMemoryIndexRepository(client, encoder)
    ref = EvidenceRef("s", "u", "项目已启动，明天继续", turn_id="t")
    now = datetime.now(timezone.utc)
    first = MemoryObject("one", "c", "项目", (
        ObjectEvent("user", "项目已启动", date(2026, 10, 31), date(2026, 10, 31), (ref,)),
    ), "position:one", now, plans=(
        ObjectPlan("user", "明天继续", date(2026, 10, 31), date(2026, 11, 1), (ref,)),
    ), forgetfulness=0)
    second = replace(first, object_id="two", source_position_key="position:two", character_id="other")
    try:
        assert await index.batch_object_index(())
        assert encoder.calls == 0
        # 模拟上次只写入第一条；整批重试覆盖它并补齐第二条。
        assert await index.batch_object_index((first,))
        assert await index.batch_object_index((first, second))
        assert await index.batch_object_index((first, second))
        points, _ = await client.scroll(index.object_collection_name, limit=10, with_payload=True, with_vectors=True)
        assert len(points) == 4
        assert {point.id for point in points} == {
            index._object_point_id(item.object_id, key)
            for item in (first, second) for key in ("object_name", "object_content")
        }
        assert {(point.payload["object_id"], point.payload["section_key"]) for point in points} == {
            (item.object_id, key) for item in (first, second)
            for key in ("object_name", "object_content")
        }
        assert encoder.texts[:2] == [first.object, first.index_text()]
        assert {point.payload["character_id"] for point in points} == {"c", "other"}
        for point in points:
            payload = point.payload
            assert payload["kind"] == "object" and payload["object"] == "项目"
            assert payload["state"] == "staged" and payload["linked_topic_id"] is None
            assert payload["forgetfulness"] == 0
            assert payload["events"][0]["occurrence_time"] == "2026-10-31"
            assert payload["plans"][0]["target_time"] == "2026-11-01"
            assert payload["events"][0]["source_refs"][0]["message_id"] == "u"
            assert set(point.vector) == {"dense", "sparse"}
        assert not await client.collection_exists(index.collection_name)
        # 收到未确认的写入状态时不能报告成功。
        original = client.upsert
        client.upsert = AsyncMock(return_value=SimpleNamespace(status="acknowledged"))
        assert not await index.batch_object_index((first,))
        client.upsert = AsyncMock(side_effect=OSError("connection dropped"))
        with pytest.raises(MemoryIndexUnavailable) as error:
            await index.batch_object_index((first,))
        assert error.value.retryable
        client.upsert = original
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_batch_object_recall_filters_roles_pools_and_own_batch(tmp_path):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock
    from qdrant_client import models
    from module.memory_management.object.core.domain.document import MemoryObject, ObjectEvent, ObjectState
    from module.memory_management.topic.core.processing.config import ObjectRecallConfig
    from module.shared.memory.types.references import EvidenceRef
    client = AsyncQdrantClient(path=str(tmp_path/"object-recall"))
    encoder = Encoder()
    index = QdrantMemoryIndexRepository(client, encoder)
    recall = QdrantMemoryRecallRepository(client, encoder, index_repository=index)
    item = MemoryObject("new1", "c", "桌宠", (ObjectEvent("user", "项目已完成", date(2026,11,1), None,
        (EvidenceRef("s", "u", "原文"),)),), "s:1", datetime.now(timezone.utc))
    try:
        empty = await recall.batch_object_search((item,), config=ObjectRecallConfig())
        assert empty[0].hits == () and not empty[0].truncated
        second = replace(item, object_id="new2", source_position_key="s:2")
        historical = (
            replace(item, object_id="hot", source_position_key="old:1"),
            replace(item, object_id="linked", source_position_key="old:linked", state=ObjectState.LINKED, linked_topic_id="topic-1"),
            replace(item, object_id="discarded", source_position_key="old:discarded", state=ObjectState.DISCARDED),
            replace(item, object_id="cold", source_position_key="old:2", recall_tier=RecallTier.COLD),
            replace(item, object_id="archived", source_position_key="old:3", recall_tier=RecallTier.ARCHIVED),
            replace(item, object_id="other-role", source_position_key="old:4", character_id="other"),
        )
        assert item.index_text() == "桌宠\n项目已完成"
        await index.batch_object_index((item, second, *historical))
        # 旧单分区point与未知分区即使向量完全匹配，也不能混入新分区检索。
        for key in (None, "unrelated"):
            payload = {**index._object_payload(item), "object_id": "legacy:" + str(key)}
            if key is not None:
                payload["section_key"] = key
            await client.upsert(index.object_collection_name, points=[models.PointStruct(
                id=index.point_id(payload["object_id"]), payload=payload,
                vector={"dense": [1.0] * 1024, "sparse": models.SparseVector(indices=[1], values=[1.0])},
            )], wait=True)
        client.query_batch_points = AsyncMock(wraps=client.query_batch_points)
        encoder.calls=0
        encoder.texts=[]
        groups = await recall.batch_object_search((item, second), config=ObjectRecallConfig())
        requests = client.query_batch_points.call_args.kwargs["requests"]
        assert len(requests) == 8
        for request, section in zip(requests, (["object_name"] * 2 + ["object_content"] * 2) * 2, strict=True):
            assert any(condition.key == "section_key" and condition.match.value == section
                       for condition in request.filter.must)
        assert encoder.calls == 1 and encoder.texts == [item.object, item.index_text(), second.object, second.index_text()]
        assert [group.source_object_id for group in groups] == ["new1", "new2"]
        for group in groups:
            assert {hit.object_id for hit in group.hits} == {"hot"}
            assert all(hit.paths == ("name", "content") for hit in group.hits)
            assert len({hit.object_id for hit in group.hits}) == len(group.hits)
            assert not group.truncated
        linked_groups = await recall.batch_linked_topic_search((item, second), config=ObjectRecallConfig())
        assert all({hit.object_id for hit in group.hits} == {"linked"} for group in linked_groups)
        for request in client.query_batch_points.call_args.kwargs["requests"]:
            assert any(condition.key == "state" and condition.match.value == "linked"
                       for condition in request.filter.must)
        hot_only = await recall.batch_object_search((item, second), config=ObjectRecallConfig(pools=(RecallTier.HOT,)))
        assert all({hit.object_id for hit in group.hits} == {"hot"} for group in hot_only)
        cold_only = await recall.batch_object_search((item,), config=ObjectRecallConfig(pools=(RecallTier.COLD,)))
        assert {hit.object_id for hit in cold_only[0].hits} == {"cold"}
        # 即使每路只有一个名额，两类状态也不互相挤占。
        limited = await recall.batch_object_search((item, second), config=ObjectRecallConfig(limit=1, fetch_limit=1))
        assert all(group.truncated for group in limited)
        assert all({hit.object_id for hit in group.hits} == {"hot"} for group in limited)
        encoded_calls = encoder.calls
        changed = replace(historical[0], state=ObjectState.LINKED, linked_topic_id="topic-new", recall_tier=RecallTier.COLD)
        assert await index.update_object_metadata((changed,))
        assert encoder.calls == encoded_calls
        points = await client.retrieve(index.object_collection_name, ids=[
            index._object_point_id(changed.object_id, key) for key in ("object_name", "object_content")])
        assert len(points) == 2
        assert all(p.payload["state"] == "linked" and p.payload["pool"] == "cold"
                   and p.payload["linked_topic_id"] == "topic-new" for p in points)
        after = await recall.batch_object_search((item, second), config=ObjectRecallConfig())
        assert after[0].hits == ()
        extra = tuple(replace(item, object_id=f"extra-{i}", source_position_key=f"extra:{i}") for i in range(6))
        await index.batch_object_index(extra)
        coarse = await recall.batch_object_search((item, second), config=ObjectRecallConfig(
            limit=1, rrf_limit=2, fetch_limit=6))
        assert all(len(group.hits) == 2 for group in coarse)  # RRF截2；不是最终rerank的limit=1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_reranker_batches_preserves_pair_identity_and_rejects_truncation():
    import torch
    from types import SimpleNamespace
    from unittest.mock import Mock
    from module.memory_management.topic.core.processing.config import MemoryRerankConfig
    from module.memory_management.topic.core.domain.retrieval import ObjectRerankPair, TopicRerankPair
    from module.memory_management.shared.contracts.errors import MemoryModelUnavailable
    from module.memory_management.topic.core.processing.prompts.object_rerank import OBJECT_NAME_RERANK_INSTRUCTION, OBJECT_CONTENT_RERANK_INSTRUCTION
    from module.memory_management.topic.repository.rerank import QwenMemoryRerankRepository

    class Inputs(dict):
        def to(self, device):
            return self

    class Tokenizer:
        length = 8
        texts = []
        def convert_tokens_to_ids(self, token):
            return {"no": 0, "yes": 1}[token]
        def __call__(self, texts, **kwargs):
            assert kwargs["truncation"] is False
            self.texts.extend(texts)
            return Inputs(input_ids=torch.ones((len(texts), self.length), dtype=torch.long))

    model = Mock(device=torch.device("cpu"))
    model.side_effect = lambda **kwargs: SimpleNamespace(logits=torch.tensor([[[0.0, 2.0]]]).repeat(
        kwargs["input_ids"].shape[0], 1, 1))
    tokenizer = Tokenizer()
    repository = QwenMemoryRerankRepository(model, tokenizer,
        MemoryRerankConfig(device="cpu", batch_size=2, max_length=8))
    pairs = tuple(ObjectRerankPair(f"new{i}", f"old{i}", "桌宠项目\n新进展", "桌宠项目\n旧进展") for i in range(5))
    title_pair = ObjectRerankPair("new", "old", "桌宠项目", "桌面陪伴项目", "name")
    assert OBJECT_NAME_RERANK_INSTRUCTION in repository._input_text(title_pair)
    assert OBJECT_CONTENT_RERANK_INSTRUCTION in repository._input_text(pairs[0])
    assert await repository.batch_object_rerank(()) == ()
    scores = await repository.batch_object_rerank(pairs)
    assert model.call_count == 3
    title_scores = await repository.batch_object_rerank((title_pair,))
    assert title_scores[0].section_key == "name"
    topic_scores = await repository.batch_topic_rerank((TopicRerankPair("new", "topic", "新object", "名称＋身份＋当前状态"),))
    assert topic_scores[0].topic_id == "topic" and topic_scores[0].source_object_id == "new"
    from module.memory_management.topic.core.processing.prompts.topic_rerank import TOPIC_RERANK_INSTRUCTION
    assert TOPIC_RERANK_INSTRUCTION in tokenizer.texts[-1]
    assert [(item.source_object_id, item.object_id) for item in scores] == [
        (pair.source_object_id, pair.object_id) for pair in pairs]
    assert all(item.score == pytest.approx(0.880797, abs=1e-6) for item in scores)
    assert "<Query>: 桌宠项目\n新进展" in tokenizer.texts[0]
    assert "<Document>: 桌宠项目\n旧进展" in tokenizer.texts[0]
    tokenizer.length = 9
    with pytest.raises(MemoryModelUnavailable) as error:
        await repository.batch_object_rerank(pairs[:1])
    assert error.value.retryable is False
    assert model.call_count == 5
    repository.close()
    assert repository._model is repository._tokenizer is None


def test_rrf_uses_explicit_object_rank_instead_of_list_position():
    from module.memory_management.topic.core.domain.retrieval import ObjectRecallHit
    # 故意打乱输入顺序；A的dense排名是1，而非列表位置2。
    dense = [ObjectRecallHit("source", "B", 3, 100, ("name",)),
             ObjectRecallHit("source", "A", 1, 0.1, ("name",))]
    sparse = [ObjectRecallHit("source", "A", 2, 100, ("name",)),
              ObjectRecallHit("source", "B", 1, 0.1, ("name",))]
    hits = QdrantMemoryRecallRepository._weighted_rrf([dense, sparse], weights=[0.3, 0.7])
    assert isinstance(hits, list)
    assert [hit.object_id for hit in hits] == ["B", "A"]
    assert [hit.rank for hit in hits] == [1, 2]
    assert hits[0].score == pytest.approx(0.3 / 63 + 0.7 / 61)
    assert hits[1].score == pytest.approx(0.3 / 61 + 0.7 / 62)


def test_object_weighted_rrf_uses_ranks_not_raw_scores():
    from module.memory_management.topic.core.domain.retrieval import ObjectRecallHit
    dense = [ObjectRecallHit("source", cid, rank, 1000, ("name",))
             for rank, cid in enumerate(("A", "B"), 1)]
    sparse = [ObjectRecallHit("source", cid, rank, 0.01, ("name",))
              for rank, cid in enumerate(("B", "A"), 1)]
    hits = QdrantMemoryRecallRepository._weighted_rrf([dense, sparse], weights=[0.3, 0.7])
    assert hits[0].object_id == "B"
    assert hits[0].score == pytest.approx(0.7/61+0.3/62)
    dense_preferred = QdrantMemoryRecallRepository._weighted_rrf([dense, sparse], weights=[0.7, 0.3])
    assert dense_preferred[0].object_id == "A"


@pytest.mark.asyncio
async def test_topic_three_sections_write_to_real_local_qdrant(tmp_path):
    from unittest.mock import AsyncMock
    from module.memory_management.topic.core.processing.config import TopicRecallConfig
    from module.memory_management.object.core.domain.document import MemoryObject, ObjectEvent
    from module.memory_management.shared.indexing.core.domain.representation import IndexRepresentation
    from module.shared.memory.types.indexing import RepresentationKind
    from datetime import datetime, timezone
    from module.memory_management.topic.core.domain.publication import TopicWrite
    from module.memory_management.topic.core.domain.document import TopicDocument, TopicIdentity, TopicCurrentState, TopicEntry
    from module.shared.memory.types.references import EvidenceRef
    client=AsyncQdrantClient(path=str(tmp_path/"topics"))
    index=QdrantMemoryIndexRepository(client,Encoder())
    source=EvidenceRef("s","u","不要放进公开payload的原文")
    document=TopicDocument("桌宠",TopicIdentity("桌面陪伴项目",(source,)),TopicCurrentState("尚未发布",("e",)),(
        TopicEntry("e","还没有发布",None,date(2026,11,1),(source,)),))
    write=TopicWrite.from_document(document)
    ref=MemoryRevisionRef(MemoryKind.TOPIC,"topic",1,write.content_hash)
    vector=VectorDocument("topic:r1",ref,"c",document,(),(),document.topic)
    try:
        sections=write.index_representations(ref,"c",RecallTier.COLD)
        assert await index.batch_hybrid_index(vector,sections)
        assert await index.batch_hybrid_index(vector,sections)
        points,_=await client.scroll(index.collection_name,limit=10,with_vectors=True,with_payload=True)
        assert len(points)==3
        assert {point.id for point in points}=={index._point_id(item) for item in sections}
        assert {point.payload["section_key"] for point in points}=={"topic_name","topic_identity","topic_current"}
        for point in points:
            assert point.payload["kind"]=="topic" and point.payload["pool"]=="cold"
            assert point.payload["title"]=="桌宠"
            assert point.payload["topic_identity"]=="桌面陪伴项目"
            assert point.payload["current_state"]=="尚未发布"
            assert "2026-11-01" in point.payload["entries"]
            assert "不要放进公开payload" not in str(point.payload)
            assert set(point.vector)=={"dense","sparse"}
            assert point.payload["topic_id"] == "topic" and point.payload["revision"] == 1
        # 模拟残留时间线point；常规查询不能命中它。
        timeline = replace(sections[0], representation_kind=RepresentationKind.TOPIC_TIMELINE,
                           section_key="topic_timeline", text="只存在于时间线的内容")
        await index.batch_hybrid_index(vector, (timeline,))
        for character_id, topic_id, pool in (("other", "foreign", RecallTier.COLD), ("c", "hot-topic", RecallTier.HOT)):
            other_ref = replace(ref, memory_id=topic_id)
            other_vector = replace(vector, ref=other_ref, character_id=character_id)
            await index.batch_hybrid_index(other_vector, write.index_representations(other_ref, character_id, pool))
        daily, daily_sections = material()
        await index.batch_hybrid_index(daily, daily_sections)
        query = MemoryObject("new", "c", "桌宠", (ObjectEvent("user", "继续开发", date(2026,11,2), None, (source,)),),
                                "new:1", datetime.now(timezone.utc))
        recall = QdrantMemoryRecallRepository(client, index._encoder, index_repository=index)
        client.query_batch_points = AsyncMock(wraps=client.query_batch_points)
        before = len(index._encoder.texts)
        cold = await recall.batch_topic_search((query,), config=TopicRecallConfig(pools=(RecallTier.COLD,)))
        assert index._encoder.texts[before:] == [query.object, query.index_text()]
        assert len(client.query_batch_points.call_args.kwargs["requests"]) == 6
        assert {hit.section_key for hit in cold[0].hits} == {"topic_name", "topic_identity", "topic_current"}
        assert len(cold[0].hits) == 3 and {hit.topic_id for hit in cold[0].hits} == {"topic"}
        assert all(hit.revision == 1 for hit in cold[0].hits)
        hot = await recall.batch_topic_search((query,), config=TopicRecallConfig())
        assert {hit.topic_id for hit in hot[0].hits} == {"hot-topic"}
        for topic_id in ("hot-second", "hot-third"):
            new_ref = replace(ref, memory_id=topic_id)
            await index.batch_hybrid_index(replace(vector, ref=new_ref),
                write.index_representations(new_ref, "c", RecallTier.HOT))
        coarse = await recall.batch_topic_search((query,), config=TopicRecallConfig(limit=1, rrf_limit=2))
        assert len({hit.topic_id for hit in coarse[0].hits}) == 2
        assert len(coarse[0].hits) == 6  # 三分区证据保留，但同topic只占一个粗排名额
        assert await recall.batch_topic_search((), config=TopicRecallConfig()) == ()
    finally:
        await client.close()
