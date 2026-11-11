"""生成任务查询的 HTTP 响应契约。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from module.shared.context.types.visual import VisualState
from module.chat.reply.core.domain.models import GenerationFailureKind, GenerationJobStatus


class GenerationFailureResponse(BaseModel):
    """生成任务失败的 HTTP 响应契约。"""
    model_config = ConfigDict(from_attributes=True)

    kind: GenerationFailureKind
    code: str
    message: str
    retryable: bool
    occurred_at: datetime


class AcceptedReplyResponse(BaseModel):
    """生成任务接受的 HTTP 响应契约。"""
    model_config = ConfigDict(from_attributes=True)
    conversation_id: str
    turn_id: str
    message_id: str
    content: str
    visual: VisualState | None
    created_at: datetime


class GenerationJobResponse(BaseModel):
    """生成任务查询的 HTTP 响应契约。"""
    model_config = ConfigDict(from_attributes=True)
    job_id: str
    turn_id: str
    input_version: int
    status: GenerationJobStatus
    retry_count: int
    manual_retry_count: int = Field(ge=0)
    available_at: datetime
    error: GenerationFailureResponse | None
    reply: AcceptedReplyResponse | None


class GenerationJobListResponse(BaseModel):
    items: list[GenerationJobResponse]
