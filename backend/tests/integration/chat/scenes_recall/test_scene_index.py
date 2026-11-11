"""真实临时Qdrant的场景入库/检索契约；固定编码器只用于验证适配联动。"""
import json
from types import SimpleNamespace

import numpy as np
import pytest
from qdrant_client import AsyncQdrantClient
from module.chat.scenes_recall.core.domain.config import SceneRecallConfig
from module.chat.reply.core.domain.materials import PromptMessage
from module.chat.scenes_recall.core.domain.models import SceneQuery
from module.chat.scenes_recall.repository.index import QdrantSceneIndex
from module.chat.scenes_recall.repository.retrieval import QdrantSceneRecall
from module.chat.scenes_recall.repository.runtime import SceneRuntime


class TestEncoder:
    def encode(self, texts, **kwargs):
        return {"dense_vecs": np.array([[1., 0.] if "想" in text else [0., 1.] for text in texts])}


@pytest.mark.asyncio
async def test_scene_import_is_idempotent_and_recall_is_scoped(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    records = [dict(session_id=sid, idx=idx, role="character", context=[{"role": "user", "text": "想见你"}],
                    text="我也是。", expr="010", pose="a") for sid, idx in (("s1", 1), ("s1", 2), ("s2", 3))]
    (source / "010.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in records), encoding="utf-8")
    rows = QdrantSceneIndex.load_scenes(source, "character_deepseek")
    index_path = tmp_path / "qdrant"
    client = AsyncQdrantClient(path=str(index_path))
    try:
        first = await QdrantSceneIndex.import_scenes(client, rows, TestEncoder())
        assert first["indexed"] == 3
        again = await QdrantSceneIndex.import_scenes(client, rows, TestEncoder())
        assert again["indexed"] == 0
        assert again["unchanged"] == 3
        recall = QdrantSceneRecall(client, TestEncoder(), SceneRecallConfig(.75, 3, 10))
        hits = await recall.search(SceneQuery("character_deepseek", (PromptMessage("user", "想见你了"),)))
        assert len(hits) == 2  # 同场景会话去重。
        assert {hit.session_id for hit in hits} == {"s1", "s2"}
        assert all(hit.score >= .75 for hit in hits)
        assert await recall.search(SceneQuery("other", (PromptMessage("user", "想见你了"),))) == ()
        assert await recall.search(SceneQuery("character_deepseek", (PromptMessage("user", "天气"),))) == ()
    finally:
        await client.close()

    (index_path / "ready.json").write_text(json.dumps(first), encoding="utf-8")
    settings = SimpleNamespace(reply_scene_qdrant_path=index_path, reply_scene_threshold=.75,
                               reply_scene_top_k=3, reply_scene_candidates=10)
    runtime = SceneRuntime(settings, encoder_provider=TestEncoder)
    try:
        await runtime.start()
        reopened_hits = await runtime.search(SceneQuery("character_deepseek", (PromptMessage("user", "想见你了"),)))
        assert {hit.session_id for hit in reopened_hits} == {"s1", "s2"}
    finally:
        await runtime.stop()
