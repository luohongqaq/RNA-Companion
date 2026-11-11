"""SSE Events"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar
from datetime import datetime

from module.chat.conversation.core.domain.contracts import AcceptedReply
from module.chat.reply.core.domain.models import GenerationFailure



@dataclass(frozen=True, slots=True)
class ChatEvent(ABC):
    event_id: int
    created_at: datetime
    job_id: str

    @property
    @abstractmethod
    def event_type(self) -> str:
        """事件名称；由具体事件提供，用于 API 编码。"""
        ...


@dataclass(frozen=True, slots=True)
class ReplyCommittedEvent(ChatEvent):
    event_type: ClassVar[str] = "reply_committed"
    reply: AcceptedReply


@dataclass(frozen=True, slots=True)
class GenerationStateEvent(ChatEvent):
    """任务状态通知共有的输入标识和手动代次。"""
    conversation_id: str
    turn_id: str
    input_version: int
    manual_retry_count: int


@dataclass(frozen=True, slots=True)
class GenerationFailedEvent(GenerationStateEvent):
    event_type: ClassVar[str] = "generation_failed"
    error: GenerationFailure


@dataclass(frozen=True, slots=True)
class GenerationCancelledEvent(GenerationStateEvent):
    event_type: ClassVar[str] = "generation_cancelled"

