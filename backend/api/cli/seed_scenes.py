"""导入角色的参考场景对白并构建本地场景索引：python -m api.cli.seed_scenes。

场景数据放在 resources/characters/<character_id>/scenes/sessions_train_by_expr/，
可用 --character 指定角色；缺省时选择第一个具备场景数据的已登记角色。
"""
import asyncio
import argparse
import json
from pathlib import Path

from qdrant_client import AsyncQdrantClient

from storage.config.settings import Settings
from storage.embedding.bge import BgeMemoryEncoder
from module.character.core.domain.resources import CharacterInfo
from module.character.repository.files import FileCharacterRepository
from module.chat.scenes_recall.repository.index import QdrantSceneIndex

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SCENE_SUBDIR = Path("scenes") / "sessions_train_by_expr"


def pick_character(character_id: str | None) -> CharacterInfo:
    """按显式参数或场景数据是否存在选择角色。"""
    characters = FileCharacterRepository.discover_sources(BACKEND_ROOT)
    if not characters:
        raise ValueError("resources/characters 下没有已登记角色")
    if character_id is not None:
        for character in characters:
            if character.character_id == character_id:
                return character
        raise ValueError(f"角色不存在: {character_id}")
    for character in characters:
        if (BACKEND_ROOT / character.card_path / SCENE_SUBDIR).is_dir():
            return character
    raise ValueError(
        "没有角色包含场景数据（resources/characters/<character_id>/scenes/sessions_train_by_expr），"
        "请先放入场景对白或使用 --character 指定角色"
    )


async def run(*, cuda: bool = False, character_id: str | None = None) -> None:
    settings = Settings()
    if settings.memory_embedding_path is None:
        raise ValueError("请配置RNA_MEMORY_EMBEDDING_PATH")
    character = pick_character(character_id)
    # 优先用角色自带的场景目录；仅当角色目录没有场景数据时回退到全局配置路径。
    source = BACKEND_ROOT / character.card_path / SCENE_SUBDIR
    if not source.is_dir() and settings.reply_scene_source_path is not None:
        source = Path(settings.reply_scene_source_path)
    if not source.is_dir():
        raise ValueError(f"场景数据目录不存在: {source}（可用 --character 指定具备场景数据的角色）")
    print(f"导入场景：角色={character.character_id}，来源={source}", flush=True)
    rows = QdrantSceneIndex.load_scenes(source, character.character_id)
    if cuda:
        from module.chat.scenes_recall.repository.embedding import CudaSceneEncoder
        encoder = await asyncio.to_thread(CudaSceneEncoder, settings.memory_embedding_path)
    else:
        encoder = await asyncio.to_thread(BgeMemoryEncoder.load_encoder, str(settings.memory_embedding_path))
    client = AsyncQdrantClient(path=str(settings.reply_scene_qdrant_path))
    try:
        (settings.reply_scene_qdrant_path / "ready.json").unlink(missing_ok=True)
        stats = await QdrantSceneIndex.import_scenes(client, rows, encoder)
        print(json.dumps(stats, ensure_ascii=False), flush=True)
        (settings.reply_scene_qdrant_path / "ready.json").write_text(json.dumps(stats), encoding="utf-8")
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--character", help="角色 ID；缺省取第一个具备场景数据的已登记角色")
    parser.add_argument("--cuda", action="store_true", help="支持CUDA的Python可加速场景批量编码")
    args = parser.parse_args()
    asyncio.run(run(cuda=args.cuda, character_id=args.character))
