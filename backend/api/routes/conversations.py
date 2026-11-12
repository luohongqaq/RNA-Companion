"""Conversation query and lifecycle routes."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status

from api.dependencies import get_conversation_service, get_generation_query_service, get_session_seal_service
from api.schemas.generation import GenerationJobListResponse, GenerationJobResponse
from module.chat.reply.core.processing.query import GenerationQueryService
from api.schemas.conversation import (
    CreateConversationRequest, ConversationResponse, ConversationPageResponse,
    SealSessionResponse, UpdateConversationRequest,
)
from module.chat.conversation.core.processing.conversations import ConversationService
from module.chat.conversation.core.processing.seal import SessionSealService

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("/{conversation_id}/generation-jobs", response_model=GenerationJobListResponse)
async def list_generation_jobs(
    conversation_id: str,
    service: Annotated[GenerationQueryService, Depends(get_generation_query_service)],
) -> GenerationJobListResponse:
    """找回当前输入的处理中/失败任务；已完成回复通过消息历史读取。"""
    results = await service.list_for_conversation(conversation_id)
    return GenerationJobListResponse(items=[GenerationJobResponse.model_validate(item) for item in results])


@router.get("", response_model=ConversationPageResponse)
async def list_conversations(
    service: Annotated[ConversationService, Depends(get_conversation_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
    state: Literal["active", "archived"] | None = None,
    character_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> ConversationPageResponse:
    """按更新时间倒序分页；不传筛选条件时返回所有状态、角色的会话。"""
    result = await service.list(
        limit=limit, cursor=cursor, state=state, character_id=character_id,
    )
    return ConversationPageResponse(
        items=[ConversationResponse.model_validate(item) for item in result.items],
        next_cursor=result.next_cursor,
    )


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: CreateConversationRequest,
    response: Response,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> ConversationResponse:
    result = await service.create(character_id=payload.character_id, title=payload.title,
                                  with_session=payload.with_session)
    response.headers["Location"] = f"/api/v1/conversations/{result.conversation_id}"
    return ConversationResponse.model_validate(result)


@router.get("/{conversation_id}", response_model=ConversationResponse, status_code=status.HTTP_200_OK)
async def get_conversation(
    conversation_id: str,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> ConversationResponse:
    result = await service.get(conversation_id)
    return ConversationResponse.model_validate(result)

@router.patch("/{conversation_id}", response_model=ConversationResponse, status_code=status.HTTP_200_OK)
async def update_conversation(
    conversation_id: str,
    payload: UpdateConversationRequest,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> ConversationResponse:
    result = await service.update(
        conversation_id, title=payload.title,
        update_title="title" in payload.model_fields_set, state=payload.state,
    )
    return ConversationResponse.model_validate(result)

@router.post(
    "/{conversation_id}/sessions/{session_id}/seal", response_model=SealSessionResponse,
    status_code=status.HTTP_200_OK,
)
async def seal_session(
    conversation_id: str,
    session_id: str,
    service: Annotated[SessionSealService, Depends(get_session_seal_service)],
    response: Response,
) -> SealSessionResponse:
    """封存指定会话；摘要已就绪时立即换窗口，仍在生成时返回 202。"""
    result = await service.seal(conversation_id, session_id)
    if result.current_session_id is None:
        response.status_code = status.HTTP_202_ACCEPTED
    return SealSessionResponse(
        conversation_id=result.conversation_id,
        sealed_session_id=result.sealed_session_id,
        current_session_id=result.current_session_id,
        pending=result.current_session_id is None,
    )
