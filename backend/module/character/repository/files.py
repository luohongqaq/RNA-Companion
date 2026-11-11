"""读取角色清单和图片，按版本原子更新提示词文件。"""
import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import ValidationError

from module.shared.character.card_render import CharacterCardRenderer
from module.shared.character.document import CharacterCardDocument
from module.shared.common.errors.base import InvalidStateError
from module.character.core.domain.errors import CharacterResourceError
from module.character.core.domain.prompts import CharacterPrompt, PromptType
from module.character.core.domain.resources import CharacterInfo, CharacterResourceManifest
from storage.sqlite.db_session import SQLiteDatabase

ROLE_FILES = {"character_card": "角色卡.md", "system_prompt": "系统提示词.md", "tone_prompt": "语气提示词.md"}


class FileCharacterRepository:
    """在角色目录内读取资源，使用 SQLite 写事务协调提示词保存。"""

    def __init__(self, database: SQLiteDatabase, root: Path | None = None) -> None:
        self._database = database
        self._root = (root or Path(__file__).resolve().parents[3]).resolve()

    @staticmethod
    def discover_sources(root: Path) -> tuple[CharacterInfo, ...]:
        """扫描 resources/characters/*/manifest.json，返回已登记角色（按路径排序，顺序稳定）。"""
        characters: dict[str, CharacterInfo] = {}
        for path in sorted((root / "resources" / "characters").glob("*/manifest.json")):
            try:
                manifest = CharacterResourceManifest.model_validate_json(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                raise CharacterResourceError(f"无法读取角色清单: {path}") from exc
            if (not manifest.character_id.strip() or not manifest.name or not manifest.name.strip()
                    or manifest.user_appellation is None):
                raise CharacterResourceError(f"角色清单必须提供 character_id、name 和 user_appellation: {path}")
            if manifest.character_id in characters:
                raise CharacterResourceError(f"角色清单中的 ID 重复: {manifest.character_id} ({path})")
            card = path.parent / ROLE_FILES["character_card"]
            try:
                card_text = card.read_bytes().decode("utf-8-sig")
            except (OSError, UnicodeError) as exc:
                raise CharacterResourceError(f"无法读取角色卡: {card}") from exc
            try:
                CharacterCardDocument.validate_selection_sections(card_text)
            except ValueError as exc:
                raise CharacterResourceError(f"{manifest.character_id}: {exc}") from exc
            characters[manifest.character_id] = CharacterInfo(
                manifest.character_id, manifest.name, path.parent.relative_to(root).as_posix(),
                manifest.user_appellation,
            )
        return tuple(characters.values())

    def discover_character_sources(self) -> tuple[CharacterInfo, ...]:
        """从项目角色目录的清单读取待登记的角色。"""
        return self.discover_sources(self._root)

    def _prompt_path(self, character: CharacterInfo, prompt_type: PromptType) -> Path:
        """只允许访问固定的三份文件；相对角色目录以项目根目录解析。"""
        filename = ROLE_FILES.get(prompt_type)
        if filename is None or not character.card_path or not character.card_path.strip():
            raise CharacterResourceError("角色提示词类型或资源目录无效")
        directory = Path(character.card_path)
        if not directory.is_absolute():
            directory = self._root / directory
        directory = directory.resolve()
        path = directory / filename
        if path.resolve().parent != directory:
            raise CharacterResourceError("角色提示词路径无效")
        return path

    def _get_prompt(self, character: CharacterInfo, prompt_type: PromptType) -> CharacterPrompt:
        path = self._prompt_path(character, prompt_type)
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8-sig").replace("\r\n", "\n")
            if not content.strip():
                raise ValueError("empty prompt")
        except (OSError, UnicodeError, ValueError) as exc:
            raise CharacterResourceError("无法读取角色提示词") from exc
        return CharacterPrompt(prompt_type, content, hashlib.sha256(raw).hexdigest())

    def read_rendered_card(self, character: CharacterInfo) -> tuple[CharacterPrompt, CharacterResourceManifest | None]:
        """为模型读取角色卡及其渲染所用的清单，保留源文件版本。"""
        source = self._get_prompt(character, "character_card")
        renderer = CharacterCardRenderer()
        directory = self._prompt_path(character, "character_card").parent
        has_manifest = (directory / "manifest.json").is_file()
        manifest = self._load(character) if has_manifest or renderer.needs_manifest(source.content) else None
        try:
            content = renderer.render(source.content, manifest, character.user_appellation)
        except ValueError as exc:
            raise CharacterResourceError(f"角色卡渲染失败: {character.character_id}") from exc
        return CharacterPrompt(source.prompt_type, content, source.version), manifest

    async def get_character_prompt(self, character: CharacterInfo, prompt_type: PromptType) -> CharacterPrompt:
        return await asyncio.to_thread(self._get_prompt, character, prompt_type)

    async def save_character_prompt(self, character: CharacterInfo, prompt_type: PromptType,
                                    content: str, version: str) -> CharacterPrompt:
        # SQLite 写事务串行化本应用的保存请求，确保比较版本和替换文件之间无其他保存插入。
        async with self._database.transaction() as session:
            await session.connection()
            task = asyncio.create_task(asyncio.to_thread(self._save_prompt, character, prompt_type, content, version))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # 客户端断开时也先等待线程结束，再释放事务锁。
                await task
                raise

    def _save_prompt(self, character: CharacterInfo, prompt_type: PromptType,
                     content: str, version: str) -> CharacterPrompt:
        current = self._get_prompt(character, prompt_type)
        if current.version != version:
            raise InvalidStateError("提示词已被修改，请重新读取后保存")
        path = self._prompt_path(character, prompt_type)
        temporary = None
        try:
            # 同一目录中先完整写入临时文件，再原子替换，避免读到半份正文。
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".prompt-", dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            return self._get_prompt(character, prompt_type)
        except OSError as exc:
            raise CharacterResourceError("无法保存角色提示词") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def load_character_resources(self, character: CharacterInfo) -> CharacterResourceManifest:
        """读取角色资源manifest:IO操作放进线程池"""
        return await asyncio.to_thread(self._load, character)

    async def read_character_file(self, character: CharacterInfo, file: str) -> bytes:
        """读取角色资源文件（具体图片）:IO操作放进线程池"""
        return await asyncio.to_thread(self._read_file, character, file)

    def _read_file(self, character: CharacterInfo, file: str) -> bytes:
        """校验资源路径并读取图片字节；路径无效或读取失败时抛资源错误。"""
        if not character.card_path or not character.card_path.strip():
            raise CharacterResourceError("角色未配置资源目录")
        # card_path 是相对路径时，以后端项目根目录为基准；绝对路径直接使用。
        directory = Path(character.card_path)
        if not directory.is_absolute():
            directory = self._root / directory
        directory = directory.resolve()
        # 清单路径只能指向 background/expression 子目录中的 PNG，不能包含盘符或 ..。
        path = PurePosixPath(file)
        if ("\\" in file or path.is_absolute() or PureWindowsPath(file).drive
                or any(part in (".", "..") for part in file.split("/"))
                or not path.parts or path.parts[0] not in {"background", "expression"}
                or path.suffix.lower() != ".png"):
            raise CharacterResourceError("资源路径无效")
        # 解析实际路径后再校验归属，防止路径链接导致文件落在允许的目录之外。
        resolved = (directory / file).resolve()
        allowed = (directory / path.parts[0]).resolve()
        if not allowed.is_relative_to(directory) or not resolved.is_relative_to(allowed):
            raise CharacterResourceError("资源路径超出目录")
        try:
            return resolved.read_bytes()  # 返回 PNG 的二进制内容，由 API 构造 HTTP 响应
        except OSError as exc:
            raise CharacterResourceError("无法读取角色资源") from exc

    def _load(self, character: CharacterInfo) -> CharacterResourceManifest:
        """读取并校验 manifest.json；缺失或无效时抛资源错误。"""
        if not character.card_path or not character.card_path.strip():
            raise CharacterResourceError("角色未配置资源目录")
        directory = Path(character.card_path)
        if not directory.is_absolute():
            directory = self._root / directory
        directory = directory.resolve()
        try:
            # 先读取 JSON 文本并解析为字典，再按 Pydantic 模型转换和检查字段。
            raw = json.loads((directory / "manifest.json").read_text(encoding="utf-8-sig"))
            resources = CharacterResourceManifest.model_validate(raw)
            # 字段格式正确后，进一步检查文件、默认值和资源间的引用关系。
            self._validate(directory, character, resources)
            return resources
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise CharacterResourceError(f"角色资源清单无效: {character.character_id}") from exc

    @staticmethod
    def _validate(directory: Path, character: CharacterInfo, data: CharacterResourceManifest) -> None:
        """验证清单引用的图片、名称和默认组合，避免返回无法使用的资源。"""
        if data.schema_version != 1 or data.file_base != "character_directory":
            raise ValueError("不支持的清单版本或路径基准")
        if data.character_id != character.character_id:
            raise ValueError("清单角色 ID 与数据库不一致")

        def check_file(value: str, subdir: str) -> None:
            # 清单里的路径相对于角色根目录；既检查格式，也检查文件实际位置和存在性。
            path = PurePosixPath(value)
            windows = PureWindowsPath(value)
            if (not value or "\\" in value or path.is_absolute() or windows.drive
                    or any(part in ("..", ".") for part in value.split("/"))
                    or not path.parts or path.parts[0] != subdir or path.suffix.lower() != ".png"):
                raise ValueError(f"资源路径无效: {value}")
            allowed = (directory / subdir).resolve()
            resolved = (directory / value).resolve()
            if not allowed.is_relative_to(directory) or not resolved.is_relative_to(allowed) or not resolved.is_file():
                raise ValueError(f"资源文件不存在或超出目录: {value}")

        # 背景 ID 和文件名必须唯一，默认 ID 必须存在。
        backgrounds = data.backgrounds
        ids = [item.id for item in backgrounds.items]
        names = [PurePosixPath(item.file).name for item in backgrounds.items]
        if len(ids) != len(set(ids)) or len(names) != len(set(names)) or backgrounds.default_id not in ids:
            raise ValueError("背景 ID 重复或默认背景不存在")
        for item in backgrounds.items:
            check_file(item.file, "background")

        # 模型通过中文名查找 ID，名称重复会导致无法确定应选哪个 ID。
        expressions = data.expression
        pose_names = [pose.name for pose in expressions.poses.values()]
        if (len(set(pose_names)) != len(pose_names)
                or len({x.name for x in expressions.expressions.values() if x.enabled})
                != sum(x.enabled for x in expressions.expressions.values())):
            raise ValueError("姿势或启用的表情名称重复")
        # 去重并确认每个立绘组合都引用了已声明的服装、姿势、表情。
        ids, combinations, files, filenames = set(), set(), set(), set()
        enabled = set()
        for item in expressions.assets:
            combo = (item.outfit_id, item.pose_id, item.expression_id)
            if (not item.asset_id or item.asset_id in ids or combo in combinations or item.file in files
                    or PurePosixPath(item.file).name in filenames
                    or item.outfit_id not in expressions.outfits or item.pose_id not in expressions.poses
                    or item.expression_id not in expressions.expressions):
                raise ValueError("立绘 ID、组合、路径重复或引用不存在")
            if item.enabled and not expressions.expressions[item.expression_id].enabled:
                raise ValueError("启用的立绘引用了禁用的表情")
            check_file(item.file, "expression")
            ids.add(item.asset_id)
            combinations.add(combo)
            files.add(item.file)
            filenames.add(PurePosixPath(item.file).name)
            if item.enabled:
                enabled.add(combo)
        # 角色默认和每套服装的默认组合必须实际存在且启用，才能保证回退可用。
        default = expressions.default
        if (default.outfit_id, default.pose_id, default.expression_id) not in enabled:
            raise ValueError("默认立绘不存在或已禁用")
        for outfit_id, outfit in expressions.outfits.items():
            if (outfit_id, outfit.default_pose_id, outfit.default_expression_id) not in enabled:
                raise ValueError(f"服装默认立绘不存在或已禁用: {outfit_id}")
