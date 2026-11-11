"""模型连接配置的业务类型；密钥不属于响应。"""
from dataclasses import dataclass, field
from typing import Literal

ModelPurpose = Literal["reply", "default"]


@dataclass(frozen=True, slots=True)
class ModelSettings:
    purpose: ModelPurpose
    base_url: str
    model: str
    api_key_configured: bool
    effective_base_url: str
    effective_model: str
    effective_api_key_configured: bool
    overridden_fields: tuple[str, ...]
    restart_required: bool
    version: str


@dataclass(frozen=True, slots=True)
class ModelSettingsUpdate:
    version: str
    # 未提供的字段不修改；api_key=None 表示清除密钥。
    changes: dict[str, str | None] = field(repr=False)
