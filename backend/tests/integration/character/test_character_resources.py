import asyncio
import json
from pathlib import Path

import pytest

from module.character.core.processing.service import CharacterService
from module.chat.reply.repository.prompt_resources import FileReplyPromptResourceReader
from module.shared.common.errors.base import NotFoundError
from storage.sqlite.db_session import SQLiteDatabase
from module.character.core.domain.errors import CharacterResourceError
from module.character.repository.files import FileCharacterRepository
from module.character.repository.sqlite import SQLiteCharacterRepository
from storage.sqlite.models import CharacterRow


@pytest.mark.asyncio
async def test_character_service_reads_registered_bundle(tmp_path):
    root = Path(__file__).resolve().parents[3]
    db = SQLiteDatabase(tmp_path / "test.sqlite")
    try:
        await db.initialize()
        repository = SQLiteCharacterRepository(db)
        await asyncio.gather(repository.register_manifest_characters(root),
                             repository.register_manifest_characters(root))
        service = CharacterService(repository, FileCharacterRepository(db, root))
        registered = {character.character_id: character for character in await service.list()}
        assert registered["character_deepseek"].name == "deepseek"
        assert registered["character_deepseek"].user_appellation == "主人"
        assert registered["character_deepseek"].card_path == "resources/characters/character_deepseek"
        data = await service.get_resources("character_deepseek")
        reader = FileReplyPromptResourceReader(db, root)
        prompt_resources = await reader.load("character_deepseek")
        assert prompt_resources.scene_recall_enabled is False
        assert prompt_resources.visual_options == data.expression.visual_options()
        assert "poses:begin" not in prompt_resources.character_card.content
        assert data.backgrounds.default_id == "bedroom"
        assert len(data.expression.assets) == 12
        assert sum(asset.enabled for asset in data.expression.assets) == 12
        with pytest.raises(NotFoundError):
            await service.get_resources("missing")
        async with db.transaction() as session:
            row = await session.get(CharacterRow, "character_deepseek")
            row.name = "自定义名称"
            row.user_appellation = "自定义称呼"
            row.card_path = "resources/custom_character"
        await repository.register_manifest_characters(root)
        character = await service.get("character_deepseek")
        assert (character.name, character.user_appellation, character.card_path) == (
            "自定义名称", "自定义称呼", "resources/custom_character",
        )
        assert len(await service.list()) == len(registered)
    finally:
        await db.close()


def test_discovery_rejects_card_missing_required_sections(tmp_path):
    root = tmp_path
    directory = root / "resources" / "characters" / "character_bad"
    directory.mkdir(parents=True)
    manifest = json.loads((Path(__file__).resolve().parents[3]
                           / "resources/characters/character_deepseek/manifest.json").read_text(encoding="utf-8"))
    manifest["character_id"] = "character_bad"
    manifest["name"] = "bad"
    manifest["user_appellation"] = "你"
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (directory / "角色卡.md").write_text("## 身份\n\n只有一段，缺少其余必需标题。\n", encoding="utf-8")
    with pytest.raises(CharacterResourceError) as excinfo:
        FileCharacterRepository(None, root).discover_character_sources()
    message = str(excinfo.value)
    assert "character_bad" in message
    assert "人格使用边界" in message and "日常陪伴基调" in message


@pytest.mark.asyncio
async def test_manifest_rejects_unsafe_missing_and_duplicate_resources(tmp_path):
    root = Path(__file__).resolve().parents[3] / "resources/characters/character_deepseek"
    data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    # Keep one valid expression and outfit to isolate each failure.
    expression = data["expression"]
    expression["assets"] = [expression["assets"][0]]
    asset = expression["assets"][0]
    expression["default"] = {k: asset[k] for k in ("outfit_id", "pose_id", "expression_id")}
    expression["outfits"] = {asset["outfit_id"]: expression["outfits"][asset["outfit_id"]]}
    for entry in data["backgrounds"]["items"] + expression["assets"]:
        path = tmp_path / entry["file"]
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(b"png")
    from module.character.core.domain.resources import CharacterInfo
    reader = FileCharacterRepository(None)
    record = CharacterInfo("character_deepseek", "鲸鱼娘", str(tmp_path))
    manifest = tmp_path / "manifest.json"

    def save():
        manifest.write_text(json.dumps(data), encoding="utf-8")

    save()
    await reader.load_character_resources(record)
    original = asset["file"]
    asset["file"] = "expression/../../outside.png"
    save()
    with pytest.raises(CharacterResourceError):
        await reader.load_character_resources(record)
    asset["file"] = "expression/missing.png"
    save()
    with pytest.raises(CharacterResourceError):
        await reader.load_character_resources(record)
    asset["file"] = original
    expression["assets"].append(dict(asset))
    save()
    with pytest.raises(CharacterResourceError):
        await reader.load_character_resources(record)
