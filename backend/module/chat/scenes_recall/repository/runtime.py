"""独立场景索引生命周期；可复用已经加载的记忆BGE编码器。"""
import asyncio
from qdrant_client import AsyncQdrantClient
from module.chat.scenes_recall.core.domain.config import SceneRecallConfig
from module.chat.scenes_recall.core.domain.errors import SceneRecallUnavailable
from module.chat.scenes_recall.core.domain.models import SceneExample, SceneQuery
from storage.embedding.bge import BgeMemoryEncoder
from module.chat.scenes_recall.repository.schema import SCENE_COLLECTION
from module.chat.scenes_recall.repository.retrieval import QdrantSceneRecall


class SceneRuntime:
    def __init__(self, settings, encoder_provider=None):
        self.settings, self.encoder_provider = settings, encoder_provider
        self.client = None
        self.recall = None

    async def start(self):
        path = self.settings.reply_scene_qdrant_path
        if not path.is_dir() or not (path / "meta.json").is_file():
            raise ValueError("场景索引尚未入库，请先运行 python -m api.cli.seed_scenes")
        if not (path / "ready.json").is_file():
            raise ValueError("场景索引尚未完整导入，请等待 python -m api.cli.seed_scenes 完成")
        self.client = AsyncQdrantClient(path=str(path))
        if not await self.client.collection_exists(SCENE_COLLECTION):
            raise ValueError("场景collection不存在，请先运行 python -m api.cli.seed_scenes")
        encoder = self.encoder_provider() if self.encoder_provider else await asyncio.to_thread(
            BgeMemoryEncoder.load_encoder, str(self.settings.memory_embedding_path))
        self.recall = QdrantSceneRecall(self.client, encoder, SceneRecallConfig(
            self.settings.reply_scene_threshold, self.settings.reply_scene_top_k, self.settings.reply_scene_candidates))

    async def search(self, query: SceneQuery) -> tuple[SceneExample, ...]:
        # 备料阶段的场景召回入口；还没启动就报装配错误
        if self.recall is None:
            raise SceneRecallUnavailable("场景召回尚未启动")
        return await self.recall.search(query)

    async def stop(self):
        self.recall = None
        if self.client is not None:
            await self.client.close()
            self.client = None
