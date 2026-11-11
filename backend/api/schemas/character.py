"""角色和资源查询的 HTTP 契约。"""
from pydantic import BaseModel, ConfigDict, Field
from module.character.core.domain.prompts import PromptType


class CharacterResponse(BaseModel):
    """角色列表中的基本信息，不包含服务器上的资源目录。"""
    character_id: str
    name: str


class CharacterListResponse(BaseModel):
    """角色列表响应；items 为空时表示没有已登记角色。"""
    items: list[CharacterResponse]


class BackgroundResponse(BaseModel):
    """背景的前端信息：id 用于选择，url 用于通过 HTTP 请求图片。"""
    id: str
    name: str
    url: str


class ExpressionAssetResponse(BaseModel):
    """一张可用立绘的 ID、所属组合和图片 URL。"""
    asset_id: str
    outfit_id: str
    pose_id: str
    expression_id: str
    url: str


class OutfitResponse(BaseModel):
    """服装名称及该服装下回退时使用的默认姿势、表情。"""
    name: str
    default_pose_id: str
    default_expression_id: str


class ExpressionSelectionResponse(BaseModel):
    """默认立绘组合，三个 ID 一起确定服装、姿势和表情。"""
    outfit_id: str
    pose_id: str
    expression_id: str


class CharacterAssetsResponse(BaseModel):
    """资源列表响应；poses、expressions 分别将 ID 映射到中文名称。"""
    character_id: str
    default_background_id: str
    backgrounds: list[BackgroundResponse]
    default_expression: ExpressionSelectionResponse
    outfits: dict[str, OutfitResponse]
    poses: dict[str, str]
    expressions: dict[str, str]
    assets: list[ExpressionAssetResponse]


class CharacterDetailResponse(CharacterResponse):
    """角色基本信息及默认背景、立绘的 URL 路径。"""
    default_background_url: str
    default_expression_url: str


class ExpressionMatchRequest(BaseModel):
    """匹配请求：服装来自用户选择，姿势和表情名称来自模型输出。"""
    # 拒绝多余字段，去掉字符串首尾空白；Field 限制字符串长度。
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    outfit_id: str = Field(min_length=1, max_length=128)
    pose_name: str = Field(min_length=1, max_length=200)
    expression_name: str = Field(min_length=1, max_length=200)


class ExpressionMatchResponse(BaseModel):
    """同时返回原始请求和实际匹配的图片，供前端判断是否发生回退。"""
    request: ExpressionMatchRequest  # 本次匹配的输入条件
    image: ExpressionAssetResponse  # 实际使用的立绘组合及其图片 URL
    fallback_used: bool  # True 表示请求的精确组合未使用


class CharacterPromptResponse(BaseModel):
    """一份提示词正文及其版本；版本需在保存时原样提交。"""
    model_config = ConfigDict(from_attributes=True)
    prompt_type: PromptType
    content: str
    version: str


class CharacterPromptUpdate(BaseModel):
    """替换一份提示词：version 是读取时获得的内容版本。"""
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=1_000_000)
    version: str = Field(pattern=r"^[0-9a-f]{64}$")
