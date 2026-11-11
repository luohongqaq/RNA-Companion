"""角色资源清单的数据契约。"""
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class ManifestModel(BaseModel):
    """清单模型公共配置：拒绝未声明字段，禁止对模型属性重新赋值。"""
    model_config = ConfigDict(extra="forbid", frozen=True)


class BackgroundEntry(ManifestModel):
    """一张背景：id 用于选择，name 用于展示，file 是相对角色根目录的路径。"""
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    file: str = Field(min_length=1)


class BackgroundCatalog(ManifestModel):
    """背景清单；default_id 必须指向 items 中的某个背景。"""
    default_id: str = Field(min_length=1)
    items: tuple[BackgroundEntry, ...]


class ExpressionSelection(ManifestModel):
    """用服装、姿势和表情三个 ID 标识一个立绘组合。"""
    outfit_id: str
    pose_id: str
    expression_id: str


class OutfitDefinition(ManifestModel):
    """用户可选服装及默认姿势、表情；source_code 供追溯原文件名。"""
    name: str
    source_code: str
    default_pose_id: str
    default_expression_id: str


class PoseDefinition(ManifestModel):
    """一个姿势：中文名供模型输出，描述说明实际画面，用于生成角色卡的允许列表。"""
    name: str = Field(min_length=1)
    description: str = ""


class ExpressionDefinition(ManifestModel):
    """表情的中文名、使用说明、原始代码和启用状态；禁用表情不参与匹配。"""
    name: str
    description: str = ""  # 什么时候用这个表情；老清单不带此字段也能通过校验
    source_code: str
    enabled: StrictBool


class ExpressionImage(ExpressionSelection):
    """实际立绘记录；继承的三个 ID 标识组合，file 是相对角色根目录的路径。"""
    asset_id: str
    file: str
    source_filename: str  # 重命名前的文件名，用于追溯
    enabled: StrictBool  # false 表示不参与匹配或图片接口


class ExpressionFallbackPolicy(ManifestModel):
    """清单中描述的回退顺序；实际顺序由业务服务执行。"""
    description: str
    order: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VisualOptions:
    poses: tuple[str, ...]
    expressions: tuple[str, ...]


class ExpressionCatalog(ManifestModel):
    """立绘配置，包括名称映射、默认组合和实际存在的图片。"""
    filename_template: str  # 命名约定；匹配时以 assets 中的记录为准
    default: ExpressionSelection  # 全角色默认组合，也是最后一级回退
    outfits: dict[str, OutfitDefinition]  # 服装 ID -> 服装配置
    poses: dict[str, PoseDefinition]  # 姿势 ID -> 名称与画面描述
    expressions: dict[str, ExpressionDefinition]  # 表情 ID -> 中文名及启用状态
    fallback_policy: ExpressionFallbackPolicy  # 回退规则说明
    assets: tuple[ExpressionImage, ...]  # 登记的图片；不是所有组合都一定存在

    def available_poses(self) -> tuple[PoseDefinition, ...]:
        pose_ids = {asset.pose_id for asset in self.assets if asset.enabled}
        return tuple(pose for pose_id, pose in self.poses.items() if pose_id in pose_ids)

    def available_expressions(self) -> tuple[ExpressionDefinition, ...]:
        return tuple(expression for expression in self.expressions.values() if expression.enabled)

    def visual_options(self) -> VisualOptions:
        return VisualOptions(
            tuple(pose.name for pose in self.available_poses()),
            tuple(expression.name for expression in self.available_expressions()),
        )


class CharacterResourceManifest(ManifestModel):
    """角色根目录 manifest.json 的完整数据结构。"""
    schema_version: int  # 清单格式版本
    character_id: str  # 必须与数据库查询的角色 ID 一致
    name: str | None = None
    user_appellation: str | None = None
    scene_recall_enabled: StrictBool = False
    file_base: str  # 文件路径基准，当前为 character_directory
    backgrounds: BackgroundCatalog  # 背景及默认背景
    expression: ExpressionCatalog  # 立绘及服装、姿势、表情映射
