"""Generation status query routes. Replies are never streamed token by token."""

from typing import Annotated
from fastapi import APIRouter, Depends, status
from api.dependencies import get_generation_query_service
from module.chat.reply.core.processing.query import GenerationQueryService
from api.schemas.generation import GenerationJobResponse

router = APIRouter(prefix="/generation-jobs", tags=["generation"])


@router.get("/{job_id}", response_model=GenerationJobResponse)
async def get_generation_job(
    job_id: str, service: Annotated[GenerationQueryService, Depends(get_generation_query_service)],
) -> GenerationJobResponse:
    """获取job的结果"""
    return GenerationJobResponse.model_validate(await service.get(job_id))

@router.post("/{job_id}/retry", response_model=GenerationJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def retry_generate_reply(
    job_id: str, service: Annotated[GenerationQueryService, Depends(get_generation_query_service)],
) -> GenerationJobResponse:
    """重新生成回复"""
    return GenerationJobResponse.model_validate(await service.retry(job_id))


@router.post("/{job_id}/cancel", response_model=GenerationJobResponse)
async def cancel_generation_job(
    job_id: str, service: Annotated[GenerationQueryService, Depends(get_generation_query_service)],
) -> GenerationJobResponse:
    """取消当前生成，保留用户消息；已完成返回 409，重复取消返回原状态。"""
    return GenerationJobResponse.model_validate(await service.cancel(job_id))


