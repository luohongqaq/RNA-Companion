"""chat.contracts: cohesive domain types; no external I/O."""
from __future__ import annotations

from module.shared.context.types.visual import VisualState
from module.chat.conversation.core.domain.models import ConversationState
from module.shared.context.types.messages import MessageListItem
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SaveMessageInput:
    """保存消息输入"""
    request_id: str
    conversation_id: str
    character_id: str
    content: str
    client_created_at: datetime | None = None #客户端发送的时间

    def validate(self) -> None:
        """检验输入"""
        for field_name in (
                "request_id",
                "conversation_id",
                "character_id",
                "content",
        ):
            value = getattr(self, field_name)
            # 输入是否为空
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} is required")
        # 内容长度是否超过限制
        if len(self.content) > 100_000:
            raise ValueError("content cannot exceed 100000 characters")
        # 客户端发送时间是否为空
        if self.client_created_at is not None:
            # 客户端发送时间是否为datetime类型
            if not isinstance(self.client_created_at, datetime):
                raise ValueError("client_created_at must be a datetime")
            # 客户端发送时间是否包含时区信息
            if self.client_created_at.utcoffset() is None:
                raise ValueError("client_created_at must include a timezone")

@dataclass(frozen=True, slots=True)
class CreateConversation:
    """创建对话；with_session 为真时同时开好第一个空会话。"""
    conversation_id: str
    character_id: str
    title: str | None
    with_session: bool = False


@dataclass(frozen=True, slots=True)
class UpdateConversation:
    conversation_id: str
    title: str | None = None
    update_title: bool = False
    state: ConversationState | None = None


@dataclass(frozen=True, slots=True)
class RenameConversationTitle:
    """重命名对话标题"""
    conversation_id: str
    title: str | None


@dataclass(frozen=True, slots=True)
class ArchiveConversation:
    """归档对话"""
    conversation_id: str


@dataclass(frozen=True, slots=True)
class SealSession:
    """封存一段会话，让已发生的对话进入记忆生产，并由同一次操作开启后继会话。"""
    conversation_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class SealSessionResult:
    """封存结果：被封存的会话与接替它的会话；等待超时时接替会话为 None。"""
    conversation_id: str
    sealed_session_id: str
    current_session_id: str | None


@dataclass(frozen=True, slots=True)
class SaveMessageResult:
    request_id: str
    message_id: str
    turn_id: str
    input_version: int
    job_id: str
    save_state: str = "committed"
    generation_state: str = "queued"


@dataclass(frozen=True, slots=True)
class RequestReceiptResult:
    """消息保存回执"""
    operation: str #操作类型
    request_id: str #请求id
    payload_hash: str #请求体哈希
    result: SaveMessageResult #保存结果


@dataclass(frozen=True, slots=True)
class ConversationDetails:
    """对话详情"""
    conversation_id: str #对话id
    character_id: str #角色id
    title: str | None #对话标题
    state: ConversationState #状态
    current_session_id: str | None #当前会话id
    created_at: datetime #创建时间
    updated_at: datetime #更新时间


@dataclass(frozen=True, slots=True)
class ConversationMessageItem(MessageListItem):
    """聊天历史项；session_seq 是 conversation 内的持久顺序。"""
    session_id: str
    session_seq: int
    turn_seq: int


MessageCursor = tuple[int, str, int, int]


@dataclass(frozen=True, slots=True)
class AcceptedReply:
    """已正式采用的回复"""
    conversation_id: str #对话id
    turn_id: str #轮次id
    message_id: str #消息id
    content: str #回复内容
    visual: VisualState | None #视觉状态
    created_at: datetime #采用时间
