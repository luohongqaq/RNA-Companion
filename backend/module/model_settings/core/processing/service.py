"""校验模型连接信息后交给配置仓储保存。"""
from urllib.parse import urlsplit
from module.shared.common.errors.base import InvalidInputError
from module.model_settings.core.domain.ports import ModelSettingsRepository
from module.model_settings.core.domain.models import ModelPurpose, ModelSettings, ModelSettingsUpdate


class ModelSettingsService:
    def __init__(self, repository: ModelSettingsRepository) -> None:
        self._repository = repository

    async def get(self, purpose: ModelPurpose) -> ModelSettings:
        return await self._repository.get_model_settings(purpose)

    async def update(self, purpose: ModelPurpose, update: ModelSettingsUpdate) -> ModelSettings:
        if not update.changes or set(update.changes) - {"base_url", "model", "api_key"}:
            raise InvalidInputError("请至少提供一个有效配置字段")
        for name, value in update.changes.items():
            if value is None:
                if name != "api_key":
                    raise InvalidInputError("地址和模型名称不能为空")
                continue
            if (not isinstance(value, str) or not value.strip() or len(value) > 4096
                    or any(ord(c) < 32 or ord(c) == 127 for c in value) or "${" in value):
                raise InvalidInputError("模型配置字段包含无效字符或长度无效")
            if name in {"base_url", "model"} and value != value.strip():
                raise InvalidInputError("地址和模型名称首尾不能有空白")
            if name == "base_url":
                try:
                    url = urlsplit(value)
                    if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                            or url.password is not None or url.query or url.fragment or url.port == 0):
                        raise ValueError
                except ValueError as exc:
                    raise InvalidInputError("模型地址必须是有效的 HTTP 或 HTTPS 地址") from exc
        return await self._repository.update_model_settings(purpose, update)
