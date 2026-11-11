"""模型设置接口的数据结构。密钥仅允许写入。"""
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from module.model_settings.core.domain.models import ModelPurpose


class ModelSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    purpose: ModelPurpose
    base_url: str  # .env 中的保存值；缺失时为项目默认值
    model: str
    api_key_configured: bool  # .env 中是否配置；不返回密钥正文
    effective_base_url: str  # 考虑进程环境变量后，下次启动将使用的值
    effective_model: str
    effective_api_key_configured: bool
    overridden_fields: tuple[str, ...]  # 被进程环境变量覆盖的字段
    restart_required: bool  # 下次启动的有效配置与当前进程启动配置不同
    version: str


class ModelSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: str = Field(pattern=r'^[0-9a-f]{64}$')
    base_url: str | None = None
    model: str | None = None
    api_key: SecretStr | None = None  # 不传保留；显式传 null 清除
