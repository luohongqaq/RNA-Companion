"""先查询characters.card_path，再在数据库会话外读取角色目录中的三份提示词。"""

import asyncio
import hashlib
import json
from pathlib import Path

from module.character.core.domain.models import CharacterCard
from module.character.core.domain.errors import CharacterResourceError
from module.character.core.domain.resources import CharacterInfo
from module.shared.common.errors.base import NotFoundError
from module.chat.reply.core.domain.errors.preparation import PromptResourceError
from module.chat.reply.core.domain.materials import ReplyPromptResources
from module.character.repository.files import FileCharacterRepository, ROLE_FILES
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow

RESOURCE_MANIFEST = "manifest.json"


class FileReplyPromptResourceReader:
    """card_path表示目录，绝对路径直接使用，相对路径以项目根目录为基准。"""

    def __init__(self, database: SQLiteDatabase, root: Path | None = None) -> None:
        self._database = database
        self._root = (root or Path(__file__).resolve().parents[4]).resolve()
        self._characters = FileCharacterRepository(database, self._root)

    async def load(self, character_id: str) -> ReplyPromptResources:
        """按ID读取角色元数据，释放读会话后再加载文件；正文只以文件为准。"""
        async with self._database.db_session() as session:
            row = await session.get(CharacterRow, character_id)
            if row is None:
                raise NotFoundError(f"character not found: {character_id}")
            character = CharacterInfo(row.character_id, row.name, row.card_path, row.user_appellation)
        directory = character.card_path
        if not directory or not directory.strip():
            raise PromptResourceError(f"角色未配置card_path目录: {character_id}")
        path = Path(directory)
        if not path.is_absolute():
            path = self._root / path
        return await asyncio.to_thread(self._read_directory, character, path.resolve())

    def _read_directory(self, character: CharacterInfo, directory: Path) -> ReplyPromptResources:
        if not directory.is_dir():
            raise PromptResourceError(f"card_path不是可读取的角色目录: {directory}")
        texts, hashes = {}, {}
        try:
            expected = set(ROLE_FILES.values())
            actual = {path.name for path in directory.iterdir() if path.is_file()}
            missing = expected - actual
            if missing:
                raise PromptResourceError(f"角色目录缺少提示词: {', '.join(sorted(missing))}")
            # 许可与来源说明文件（LICENSE*/NOTICE*/README*）属于角色目录的合法随附文件，随第三方素材一同分发。
            unknown = {name for name in actual - expected - {RESOURCE_MANIFEST}
                       if not name.startswith(('.', 'LICENSE', 'NOTICE', 'README'))}
            if unknown:
                raise PromptResourceError(f"角色目录有未分类文件: {', '.join(sorted(unknown))}")
            for role, filename in ROLE_FILES.items():
                if role == "character_card":
                    card, manifest = self._characters.read_rendered_card(character)
                    texts[role], hashes[role] = card.content, card.version
                    continue
                raw = (directory / filename).read_bytes()
                text = raw.decode("utf-8-sig").replace("\r\n", "\n")
                if not text.strip():
                    raise PromptResourceError(f"角色提示词为空: {filename}")
                texts[role] = text
                hashes[role] = hashlib.sha256(raw).hexdigest()
        except (OSError, UnicodeError, CharacterResourceError) as exc:
            raise PromptResourceError(f"无法读取角色提示词: {directory}") from exc
        version = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        return ReplyPromptResources(
            character_card=CharacterCard(character.character_id, character.name, texts["character_card"], directory),
            character_card_version=hashes["character_card"], system_prompt=texts["system_prompt"],
            output_requirements="", template_version=version,
            tone_prompt=texts["tone_prompt"], tone_prompt_version=hashes["tone_prompt"],
            visual_options=manifest.expression.visual_options() if manifest is not None else None,
            scene_recall_enabled=manifest.scene_recall_enabled if manifest is not None else False,
        )
