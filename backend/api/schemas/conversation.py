"""会话创建与详情查询的 HTTP 契约。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CreateConversationRequest(BaseModel):
    """conversation创建的 HTTP 契约。"""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    character_id: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    with_session: bool = False

class ConversationResponse(BaseModel):
    """conversation响应的 HTTP 契约。"""
    model_config = ConfigDict(from_attributes=True)
    conversation_id: str
    character_id: str
    title: str | None
    state: Literal["active", "archived"]
    current_session_id: str | None
    created_at: datetime
    updated_at: datetime


class SealSessionResponse(BaseModel):
    """封存结果的 HTTP 契约：接替会话为空表示摘要仍在生成。"""
    conversation_id: str
    sealed_session_id: str
    current_session_id: str | None
    pending: bool


class ConversationPageResponse(BaseModel):
    items: list[ConversationResponse]
    next_cursor: str | None


class UpdateConversationRequest(BaseModel):
    """省略字段保持原值；title=null 清空标题，state 不允许 null。"""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=200)
    state: Literal["active", "archived"] | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> "UpdateConversationRequest":
        if not self.model_fields_set:
            raise ValueError("至少提供 title 或 state")
        if "state" in self.model_fields_set and self.state is None:
            raise ValueError("state 不允许为 null")
        return self
