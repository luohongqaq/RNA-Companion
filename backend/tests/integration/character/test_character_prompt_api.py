"""角色提示词编辑、并发版本与生成读取的一致性。"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from api.app import create_app
from module.character.core.processing.service import CharacterService
from module.chat.reply.repository.prompt_resources import FileReplyPromptResourceReader, ROLE_FILES
from storage.sqlite.db_session import SQLiteDatabase
from module.character.core.domain.errors import CharacterResourceError
from module.character.repository.files import FileCharacterRepository
from module.character.repository.sqlite import SQLiteCharacterRepository
from module.character.core.domain.resources import CharacterInfo
from storage.sqlite.models import CharacterRow


@pytest.mark.asyncio
async def test_prompt_edit_and_generation_reader(tmp_path):
    directory = tmp_path / "character"
    directory.mkdir()
    for kind, name in ROLE_FILES.items():
        (directory / name).write_text(f"original {kind}", encoding="utf-8")
    db = SQLiteDatabase(tmp_path / "test.sqlite")
    try:
        await db.initialize()
        async with db.transaction() as session:
            session.add(CharacterRow(character_id="c", name="角色", character_setting="",
                                     card_path=str(directory), user_appellation="小林"))
        app = create_app()
        app.state.container = SimpleNamespace(character_service=CharacterService(SQLiteCharacterRepository(db), FileCharacterRepository(db)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            url = "/api/v1/characters/c/prompts/character_card"
            old = (await client.get(url)).json()
            assert old["content"] == "original character_card"
            payload = {"version": old["version"], "content": "新角色卡\n {{ user_name }}{# 编辑说明 #}\n"}
            saved = await client.put(url, json=payload)
            assert saved.status_code == 200
            assert saved.json()["content"] == payload["content"]
            assert saved.json()["version"] != old["version"]
            assert (await client.put(url, json=payload)).status_code == 409
            assert (await client.get(url)).json() == saved.json()
            assert (await client.put(url, json={"version": saved.json()["version"], "content": "  "})).status_code == 422
            assert (await client.get("/api/v1/characters/c/prompts/other")).status_code == 422
            assert (await client.get("/api/v1/characters/missing/prompts/tone_prompt")).status_code == 404
            resources = await FileReplyPromptResourceReader(db).load("c")
            assert resources.character_card.content == "新角色卡\n 小林\n"
            assert resources.character_card_version == saved.json()["version"]
            assert resources.tone_prompt == "original tone_prompt"
            for kind in ("system_prompt", "tone_prompt"):
                other_url = f"/api/v1/characters/c/prompts/{kind}"
                previous = (await client.get(other_url)).json()
                response = await client.put(other_url, json={"version": previous["version"], "content": f"新 {kind}"})
                assert response.status_code == 200
                assert response.json()["content"] == f"新 {kind}"
            resources = await FileReplyPromptResourceReader(db).load("c")
            assert resources.system_prompt == "新 system_prompt"
            assert resources.tone_prompt == "新 tone_prompt"

            # 同一个版本同时提交两次，只允许一次写入成功。
            current = saved.json()["version"]
            results = await asyncio.gather(*[
                client.put(url, json={"version": current, "content": f"并发内容 {i}"}) for i in range(2)
            ])
            assert sorted(r.status_code for r in results) == [200, 409]
    finally:
        await db.close()


def test_prompt_replace_failure_keeps_original_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / ROLE_FILES["system_prompt"]
    path.write_text("旧内容", encoding="utf-8")
    repository = FileCharacterRepository(None)
    character = CharacterInfo("c", "角色", str(tmp_path))
    version = repository._get_prompt(character, "system_prompt").version

    def fail_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr("module.character.repository.files.os.replace", fail_replace)
    with pytest.raises(CharacterResourceError):
        repository._save_prompt(character, "system_prompt", "新内容", version)
    assert path.read_text(encoding="utf-8") == "旧内容"
    assert list(tmp_path.glob(".prompt-*")) == []
