"""SQLite 角色元数据读取及内置角色登记。"""
import asyncio
from pathlib import Path

from sqlalchemy import select

from module.character.core.domain.resources import CharacterInfo
from module.character.repository.files import FileCharacterRepository
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow


class SQLiteCharacterRepository:
    """读取角色名称与资源目录。"""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def default_character_id(self) -> str | None:
        """返回已登记角色中按 ID 排序的第一个；没有角色时返回 None。"""
        async with self._database.db_session() as session:
            row = (await session.scalars(
                select(CharacterRow).order_by(CharacterRow.character_id).limit(1)
            )).first()
            return row.character_id if row is not None else None

    async def get_character_source(self, character_id: str) -> CharacterInfo | None:
        """读取角色元数据；不存在时返回 None，文件读取在数据库会话外进行。"""
        async with self._database.db_session() as session:
            row = await session.get(CharacterRow, character_id)
            if row is None:
                return None
            return CharacterInfo(row.character_id, row.name, row.card_path, row.user_appellation)

    async def list_characters(self) -> tuple[CharacterInfo, ...]:
        """列出所有角色的元数据；文件读取在数据库会话外进行。"""
        async with self._database.db_session() as session:
            rows = (await session.scalars(select(CharacterRow).order_by(CharacterRow.character_id))).all()
            return tuple(CharacterInfo(row.character_id, row.name, row.card_path, row.user_appellation)
                         for row in rows)

    async def register_manifest_characters(self, root: Path) -> None:
        """幂等登记资源清单中的角色，保留已配置的数据库值。"""
        files = FileCharacterRepository(self._database, root)
        characters = await asyncio.to_thread(files.discover_character_sources)
        if not characters:
            return
        async with self._database.transaction() as session:
            for character in characters:
                row = await session.get(CharacterRow, character.character_id)
                if row is None:
                    session.add(CharacterRow(
                        character_id=character.character_id, name=character.name, character_setting="",
                        card_path=character.card_path, user_appellation=character.user_appellation,
                    ))
                    continue
                if not row.name.strip():
                    row.name = character.name
                if not row.user_appellation.strip():
                    row.user_appellation = character.user_appellation
                if not row.card_path or not row.card_path.strip():
                    row.card_path = character.card_path
