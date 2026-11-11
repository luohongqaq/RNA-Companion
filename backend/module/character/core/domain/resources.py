"""角色元数据及资源清单的数据契约。"""
from dataclasses import dataclass

from module.shared.character.types.manifest import (
    CharacterResourceManifest as CharacterResourceManifest,
    ExpressionImage as ExpressionImage,
)


@dataclass(frozen=True, slots=True)
class CharacterInfo:
    """characters 表中的角色信息；card_path 只供后端定位资源目录。"""
    character_id: str
    name: str
    card_path: str | None  # 绝对路径或相对项目根目录的路径；未配置时为 None
    user_appellation: str = ""  # 该角色对用户的称呼，渲染角色卡模板时替换 {{ user_name }}


@dataclass(frozen=True, slots=True)
class ExpressionMatch:
    """立绘匹配结果：实际选中的资源，以及是否偏离请求的精确组合。"""
    asset: ExpressionImage
    fallback_used: bool  # True 表示精确组合不可用，使用了后续回退组合
