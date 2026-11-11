"""角色查询、提示词编辑与立绘匹配用例。"""
from pathlib import PurePosixPath

from module.shared.common.errors.base import InvalidInputError, NotFoundError
from module.character.core.domain.ports import CharacterMetadataPort, CharacterResourcePort
from module.character.core.domain.prompts import CharacterPrompt, PromptType
from module.character.core.domain.resources import CharacterInfo, CharacterResourceManifest, ExpressionMatch


DEFAULT_CHARACTER_ID = "character_deepseek"


class CharacterService:
    def __init__(self, metadata: CharacterMetadataPort, resources: CharacterResourcePort) -> None:
        self._metadata = metadata
        self._resources = resources

    async def list(self) -> tuple[CharacterInfo, ...]:
        """列出所有角色的资源目录；默认角色固定排在首位。"""
        items = await self._metadata.list_characters()
        return tuple(sorted(items, key=lambda item: (item.character_id != DEFAULT_CHARACTER_ID, item.character_id)))

    async def get(self, character_id: str) -> CharacterInfo:
        """拿到指定角色的资源目录"""
        if not isinstance(character_id, str) or not character_id.strip():
            raise InvalidInputError("角色 ID 不能为空")
        character = await self._metadata.get_character_source(character_id)
        if character is None:
            raise NotFoundError("角色不存在")
        return character

    async def get_resources(self, character_id: str) -> CharacterResourceManifest:
        """拿到指定角色的manifest"""
        return await self._resources.load_character_resources(await self.get(character_id))

    async def get_prompt(self, character_id: str, prompt_type: PromptType) -> CharacterPrompt:
        """查询一份角色提示词及其当前版本。"""
        return await self._resources.get_character_prompt(await self.get(character_id), prompt_type)

    async def save_prompt(self, character_id: str, prompt_type: PromptType,
                          content: str, version: str) -> CharacterPrompt:
        """非空文本在版本一致时保存，保留正文原有空格和换行。"""
        if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > 1_000_000:
            raise InvalidInputError("提示词不能为空且 UTF-8 内容不能超过 100 万字节")
        return await self._resources.save_character_prompt(await self.get(character_id), prompt_type, content, version)

    async def resolve_expression(
        self, character_id: str, *, outfit_id: str, pose_name: str, expression_name: str,
    ) -> ExpressionMatch:
        """用用户选择的服装和模型输出的中文名称查找可用立绘。"""
        catalog = (await self.get_resources(character_id)).expression
        outfit = catalog.outfits.get(outfit_id)
        if outfit is None:
            raise InvalidInputError("服装不存在")
        # 用next获取对应资源，获取不到返回None，后续采用默认资源
        pose_id = next((key for key, pose in catalog.poses.items() if pose.name == pose_name), None)
        expression_id = next((key for key, expression in catalog.expressions.items()
                              if expression.enabled and expression.name == expression_name), None)
        #角色立绘资源的优先级回退选择
        # 依次尝试：精确组合、同姿势默认表情、默认姿势同表情、服装默认、角色默认。
        choices = [
            (outfit_id, pose_id, expression_id),
            (outfit_id, pose_id, outfit.default_expression_id),
            (outfit_id, outfit.default_pose_id, expression_id),
            (outfit_id, outfit.default_pose_id, outfit.default_expression_id),
            (catalog.default.outfit_id, catalog.default.pose_id, catalog.default.expression_id),
        ]
        # 在manifest中查找可用的资源；以三个 ID 组成的元组作为字典键。
        available = {(item.outfit_id, item.pose_id, item.expression_id): item
                     for item in catalog.assets if item.enabled}
        # 遍历优先级列表，查找可用的资源
        for index, choice in enumerate(choices):
            asset = available.get(choice)
            if asset is not None:
                # index 为 0 是精确匹配；大于 0 说明使用了回退。
                return ExpressionMatch(asset, fallback_used=index != 0)
        raise InvalidInputError("没有可用的角色立绘")

    async def read_asset(self, character_id: str, *, kind: str, file_name: str) -> bytes:
        """只读取清单中登记的背景或已启用立绘，返回图片字节。"""
        # 读取对应的角色资源
        character = await self.get(character_id)
        resources = await self._resources.load_character_resources(character)
        if kind == "background":
            paths = [item.file for item in resources.backgrounds.items]
        elif kind == "expression":
            paths = [item.file for item in resources.expression.assets if item.enabled]
        else:
            raise NotFoundError("资源不存在")
        # URL 只含文件名；从清单找回完整相对路径，不把请求参数直接当磁盘路径。
        matches = [path for path in paths if PurePosixPath(path).name == file_name]
        if len(matches) != 1:
            raise NotFoundError("资源不存在")
        return await self._resources.read_character_file(character, matches[0])
