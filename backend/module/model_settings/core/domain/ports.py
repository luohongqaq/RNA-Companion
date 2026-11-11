"""模型配置业务需要的持久化能力。"""
from typing import Protocol
from module.model_settings.core.domain.models import ModelPurpose, ModelSettings, ModelSettingsUpdate


class ModelSettingsRepository(Protocol):
    async def get_model_settings(self, purpose: ModelPurpose) -> ModelSettings: ...
    async def update_model_settings(self, purpose: ModelPurpose, update: ModelSettingsUpdate) -> ModelSettings: ...
