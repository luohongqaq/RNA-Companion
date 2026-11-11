"""SSE data 字段的 JSON 契约。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from api.schemas.generation import AcceptedReplyResponse, GenerationFailureResponse


class ChatEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: int = Field(gt=0)
    created_at: datetime
    job_id: str


class ReplyCommittedEventResponse(ChatEventResponse):
    reply: AcceptedReplyResponse


class GenerationStateEventResponse(ChatEventResponse):
    conversation_id: str
    turn_id: str
    input_version: int = Field(ge=1)
    manual_retry_count: int = Field(ge=0)


class GenerationFailedEventResponse(GenerationStateEventResponse):
    error: GenerationFailureResponse


class GenerationCancelledEventResponse(GenerationStateEventResponse):
    pass
