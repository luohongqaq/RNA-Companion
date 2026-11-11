"""角色查询、匹配和图片访问的 HTTP 契约。"""
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from api.app import create_app
from module.character.core.processing.service import CharacterService
from module.character.core.domain.resources import CharacterInfo
from module.character.repository.files import FileCharacterRepository


class Repository:
    def __init__(self, character):
        self.character = character

    async def list_characters(self):
        return (self.character,)

    async def get_character_source(self, character_id):
        return self.character if character_id == self.character.character_id else None

@pytest.mark.asyncio
async def test_character_assets_resolve_fallback_and_serve_images():
    root = Path(__file__).resolve().parents[3]
    character = CharacterInfo("character_deepseek", "deepseek", "resources/characters/character_deepseek")
    service = CharacterService(Repository(character), FileCharacterRepository(None, root))
    app = create_app()
    app.state.container = SimpleNamespace(character_service=service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        prefix = "/api/v1/characters/character_deepseek"
        listing = await client.get("/api/v1/characters")
        assert listing.json() == {"items": [{"character_id": "character_deepseek", "name": "deepseek"}]}
        detail = (await client.get(prefix)).json()
        assert "card_path" not in detail
        assert (await client.get(detail["default_background_url"])).headers["content-type"] == "image/png"
        assets = (await client.get(prefix + "/assets")).json()
        assert len(assets["assets"]) == 12
        assert "expr_999" not in assets["expressions"]
        payload = {"outfit_id": "outfit_01", "pose_name": "正面自然站姿", "expression_name": "微笑·开心"}
        resolved = (await client.post(prefix + "/expression/resolve", json=payload)).json()
        assert resolved["fallback_used"] is False
        assert resolved["request"] == payload
        assert resolved["image"]["expression_id"] == "expr_010"
        response = await client.get(resolved["image"]["url"])
        assert response.status_code == 200 and response.content.startswith(b"\x89PNG\r\n\x1a\n")
        payload["expression_name"] = "未知表情"
        fallback = (await client.post(prefix + "/expression/resolve", json=payload)).json()
        assert fallback["fallback_used"] is True
        assert fallback["image"]["pose_id"] == "pose_a"
        assert fallback["image"]["expression_id"] == "expr_010"
        payload["expression_name"] = "微笑·开心"
        payload["pose_name"] = "未知姿势"
        fallback = (await client.post(prefix + "/expression/resolve", json=payload)).json()
        assert fallback["fallback_used"] is True
        assert fallback["image"]["pose_id"] == "pose_a"
        assert fallback["image"]["expression_id"] == "expr_010"
        payload["outfit_id"] = "outfit_07"
        assert (await client.post(prefix + "/expression/resolve", json=payload)).status_code == 422
        payload["outfit_id"] = "invalid"
        assert (await client.post(prefix + "/expression/resolve", json=payload)).status_code == 422
        assert (await client.get(prefix + "/assets/expression/outfit_01__pose_a__expr_999.png")).status_code == 404
        assert (await client.get(prefix + "/assets/background/manifest.json")).status_code == 404
        assert (await client.get("/api/v1/characters/missing/assets")).status_code == 404
