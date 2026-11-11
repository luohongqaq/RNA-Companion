"""查询和修改 reply/default 模型连接设置。"""
from typing import Annotated
from fastapi import APIRouter, Depends, Response
from api.dependencies import get_model_settings_service
from api.schemas.model_settings import ModelSettingsResponse, ModelSettingsUpdateRequest
from module.model_settings.core.processing.service import ModelSettingsService
from module.model_settings.core.domain.models import ModelPurpose, ModelSettingsUpdate

router = APIRouter(prefix='/model-settings', tags=['model-settings'])
Service = Annotated[ModelSettingsService, Depends(get_model_settings_service)]


@router.get('/{purpose}', response_model=ModelSettingsResponse)
async def get_model_settings(purpose: ModelPurpose, response: Response, service: Service) -> ModelSettingsResponse:
    response.headers['Cache-Control'] = 'no-store'
    return ModelSettingsResponse.model_validate(await service.get(purpose))


@router.patch('/{purpose}', response_model=ModelSettingsResponse)
async def update_model_settings(purpose: ModelPurpose, payload: ModelSettingsUpdateRequest,
                                response: Response, service: Service) -> ModelSettingsResponse:
    changes = {}
    for name in payload.model_fields_set - {'version'}:
        value = getattr(payload, name)
        changes[name] = value.get_secret_value() if name == 'api_key' and value is not None else value
    result = await service.update(purpose, ModelSettingsUpdate(payload.version, changes))
    response.headers['Cache-Control'] = 'no-store'
    return ModelSettingsResponse.model_validate(result)
