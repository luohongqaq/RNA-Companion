"""用户编辑索引的查询、手动重试；与generation路由使用相同的响应转换方式。"""
from typing import Annotated

from fastapi import APIRouter, Depends, status

from api.dependencies import get_memory_edit_service
from api.schemas.memory import MemoryEditDetailsResponse
from module.user_edit.memory_edit.core.processing.edit_service import MemoryEditService

router = APIRouter(prefix="/memory-edits", tags=["memories"])


@router.get("/{edit_id}", response_model=MemoryEditDetailsResponse)
async def get_memory_edit(
    edit_id: str, service: Annotated[MemoryEditService, Depends(get_memory_edit_service)],
) -> MemoryEditDetailsResponse:
    """查询已保存编辑的索引状态，不返回内部正文和来源。"""
    return MemoryEditDetailsResponse.model_validate(await service.get_memory_edit(edit_id))


@router.post("/{edit_id}/retry", response_model=MemoryEditDetailsResponse, status_code=status.HTTP_202_ACCEPTED)
async def retry_memory_edit(
    edit_id: str, service: Annotated[MemoryEditService, Depends(get_memory_edit_service)],
) -> MemoryEditDetailsResponse:
    """只重新排队失败索引；已被新版本取代或非失败状态返回409。"""
    return MemoryEditDetailsResponse.model_validate(await service.retry_memory_edit(edit_id))
