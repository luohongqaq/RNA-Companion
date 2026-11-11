"""Message API contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict
from module.shared.context.types.visual import VisualState


class SendMessageRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=128)
    # 缺省时由路由解析为第一个已登记角色
    character_id: str | None = Field(default=None, min_length=1)
    content: str = Field(min_length=1, max_length=100_000)
    client_created_at: datetime | None = None

class SendMessageResponse(BaseModel):
    request_id: str
    message_id: str
    turn_id: str
    input_version: int
    job_id: str
    save_state: str
    generation_state: str

class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    session_id: str
    turn_id: str
    turn_seq: int
    message_id: str
    message_seq: int
    role: Literal["user", "character"]
    content: str
    visual: VisualState | None
    created_at: datetime

class MessagePageResponse(BaseModel):
    items: list[MessageResponse]
    next_cursor: str | None
