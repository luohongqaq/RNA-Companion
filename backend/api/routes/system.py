"""Health and lifecycle endpoints used by Electron."""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from api.dependencies import get_retention_service
from api.schemas.maintenance import RetentionRequest, RetentionResponse
from module.memory_management.retention.core.processing.service import MemoryRetentionService
from module.shared.common.time import BackendTime

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "architecture": "api-core-storage"}


@router.post("/shutdown")
async def shutdown(request: Request) -> dict[str, str]:
    # Electron terminates the process after this response. Flush local handles now.
    await request.app.state.container.stop()
    return {"status": "ok"}


@router.post("/maintenance/retention", response_model=RetentionResponse)
async def run_retention(
    payload: RetentionRequest,
    service: Annotated[MemoryRetentionService, Depends(get_retention_service)],
) -> RetentionResponse:
    """按给定业务日期执行一次降池；离线回放用它推进虚拟时间。"""
    target = payload.target_date or BackendTime.local(datetime.now(timezone.utc)).date()
    cooled = await service.cool_expired_memories(target)
    return RetentionResponse(cooled=cooled, target_date=target)
