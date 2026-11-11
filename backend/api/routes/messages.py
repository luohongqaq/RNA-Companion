"""Non-streaming message acceptance routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from api.dependencies import get_character_service, get_message_service
from api.schemas.message import SendMessageRequest, SendMessageResponse, MessagePageResponse
from module.chat.conversation.core.domain.contracts import SaveMessageInput
from module.chat.conversation.core.processing.messages import MessageService
from module.shared.common.errors.base import NotFoundError

router = APIRouter(tags=["messages"])

@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=SendMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_message(
    conversation_id: str,
    payload: SendMessageRequest,
    request: Request,
    service: Annotated[MessageService, Depends(get_message_service)],
) -> SendMessageResponse:
    character_id = payload.character_id
    if character_id is None:
        # 缺省时取第一个已登记角色；仅在需要时解析，避免为显式传参的请求引入额外依赖。
        registered = await get_character_service(request).list()
        if not registered:
            raise NotFoundError("尚未登记角色")
        character_id = registered[0].character_id
    result = await service.accept_message(
        SaveMessageInput(
            request_id=payload.request_id,
            conversation_id=conversation_id,
            character_id=character_id,
            content=payload.content,
            client_created_at=payload.client_created_at,
        )
    )
    return SendMessageResponse.model_validate(result)

@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=MessagePageResponse,
    status_code=status.HTTP_200_OK,
)
async def list_conversation_messages(
    conversation_id: str,
    service: Annotated[MessageService, Depends(get_message_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
) -> MessagePageResponse:
    """首次获取最近消息，游标向更早历史翻页；页内按聊天顺序返回。"""
    result = await service.list_messages(
        conversation_id=conversation_id, limit=limit, cursor=cursor,
    )
    return MessagePageResponse.model_validate(result, from_attributes=True)
