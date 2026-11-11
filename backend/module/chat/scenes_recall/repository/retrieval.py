"""场景示例召回适配器：独立处理场景检索与内容哈希核对，不承担记忆 Agent 业务。"""
from __future__ import annotations
import asyncio
import hashlib
import json
from module.chat.scenes_recall.core.domain.config import SceneRecallConfig
from module.chat.scenes_recall.core.domain.models import SceneExample, SceneQuery
from qdrant_client import models
from module.chat.scenes_recall.repository.schema import SCENE_COLLECTION, SCENE_SCHEMA
from module.chat.scenes_recall.repository.index import QdrantSceneIndex


class QdrantSceneRecall:
    def __init__(self, client, encoder, config: SceneRecallConfig | None = None):
        self.client, self.encoder = client, encoder
        self.config = config or SceneRecallConfig()

    async def search(self, query: SceneQuery) -> tuple[SceneExample, ...]:
        if not query.character_id:
            raise ValueError("场景检索缺少固定的角色ID")
        text = "\n".join(QdrantSceneIndex.scene_line(item.role, item.content) for item in query.messages[-4:])
        vector = (await asyncio.to_thread(QdrantSceneIndex.encode_scenes, [text], self.encoder))[0]
        result = await self.client.query_points(
            SCENE_COLLECTION, query=vector, query_filter=models.Filter(must=[
                models.FieldCondition(key="character_id", match=models.MatchValue(value=query.character_id)),
                models.FieldCondition(key="schema", match=models.MatchValue(value=SCENE_SCHEMA)),
            ]), limit=self.config.candidates, score_threshold=self.config.threshold, with_payload=True,
        )
        selected, sessions = [], set()
        for point in result.points:
            row = point.payload
            source = {key: value for key, value in row.items() if key not in ("content_hash", "query_text")}
            digest = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if digest != row["content_hash"]:
                raise ValueError("场景正文hash不一致，拒绝交付场景参考")
            if row["session_id"] in sessions:
                continue
            sessions.add(row["session_id"])
            selected.append(SceneExample(row["scene_id"], row["session_id"], point.score,
                                         tuple(row["context"]), row["reply"], row["content_hash"]))
            if len(selected) == self.config.top_k:
                break
        return tuple(selected)
