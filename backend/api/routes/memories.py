"""日记与主题的查询及完整正文编辑；保存成功后由后台重新索引。"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from api.dependencies import get_memory_query_service, get_memory_edit_service
from api.schemas.memory import (
    DailyMemoryListItemResponse, MemoryDetailsResponse, TopicMemoryListItemResponse,
    UpdateDailyMemoryRequest, UpdateTopicMemoryRequest, MemoryEditResponse,
)
from module.shared.common.types.pagination import Page
from module.user_edit.memory_edit.core.processing.query_service import MemoryQueryService
from module.user_edit.memory_edit.core.processing.edit_service import MemoryEditService
from module.shared.memory.types.references import MemoryKind
from module.user_edit.memory_edit.core.domain.command import UpdateDailyMemory, UpdateTopicMemory

router = APIRouter(prefix="/memories", tags=["memories"])


@router.get("/daily", response_model=Page[DailyMemoryListItemResponse])
async def list_daily_memories(
    service: Annotated[MemoryQueryService, Depends(get_memory_query_service)],
    character_id: Annotated[str, Query(min_length=1, max_length=128)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
) -> Page[DailyMemoryListItemResponse]:
    """按日记日期从早到晚展示，游标向较晚日期翻页。"""
    result = await service.list_daily_memories(character_id=character_id, limit=limit, cursor=cursor)
    return Page(items=tuple(DailyMemoryListItemResponse.model_validate(item) for item in result.items),
                next_cursor=result.next_cursor)


@router.get("/topic", response_model=Page[TopicMemoryListItemResponse])
async def list_topic_memories(
    service: Annotated[MemoryQueryService, Depends(get_memory_query_service)],
    character_id: Annotated[str, Query(min_length=1, max_length=128)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
) -> Page[TopicMemoryListItemResponse]:
    """按主题更新时间从新到旧展示，游标向较早更新时间翻页。"""
    result = await service.list_topic_memories(character_id=character_id, limit=limit, cursor=cursor)
    return Page(items=tuple(TopicMemoryListItemResponse.model_validate(item) for item in result.items),
                next_cursor=result.next_cursor)


@router.get("/daily/{document_id}", response_model=MemoryDetailsResponse)
async def get_daily_memory(
    document_id: str,
    service: Annotated[MemoryQueryService, Depends(get_memory_query_service)],
    character_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> MemoryDetailsResponse:
    result = await service.get_memory(character_id=character_id, kind=MemoryKind.DAILY, document_id=document_id)
    return MemoryDetailsResponse.model_validate(result)


@router.get("/topic/{document_id}", response_model=MemoryDetailsResponse)
async def get_topic_memory(
    document_id: str,
    service: Annotated[MemoryQueryService, Depends(get_memory_query_service)],
    character_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> MemoryDetailsResponse:
    result = await service.get_memory(character_id=character_id, kind=MemoryKind.TOPIC, document_id=document_id)
    return MemoryDetailsResponse.model_validate(result)


@router.put("/daily/{document_id}", response_model=MemoryEditResponse)
async def update_daily_memory(
    document_id: str,
    payload: UpdateDailyMemoryRequest,
    service: Annotated[MemoryEditService, Depends(get_memory_edit_service)],
) -> MemoryEditResponse:
    """完整替换已有日记正文；200表示正文已保存，索引进度见index_status。"""
    result = await service.update_daily_memory(UpdateDailyMemory(
        character_id=payload.character_id, document_id=document_id,
        expected_revision=payload.expected_revision, idempotency_key=payload.idempotency_key,
        document=payload.document,
    ))
    return MemoryEditResponse.model_validate(result)


@router.put("/topic/{document_id}", response_model=MemoryEditResponse)
async def update_topic_memory(
    document_id: str,
    payload: UpdateTopicMemoryRequest,
    service: Annotated[MemoryEditService, Depends(get_memory_edit_service)],
) -> MemoryEditResponse:
    """完整替换已有主题正文；版本或幂等冲突返回409，后台索引不阻塞保存。"""
    result = await service.update_topic_memory(UpdateTopicMemory(
        character_id=payload.character_id, document_id=document_id,
        expected_revision=payload.expected_revision, idempotency_key=payload.idempotency_key,
        document=payload.document,
    ))
    return MemoryEditResponse.model_validate(result)
