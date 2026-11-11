"""chat.models: cohesive domain types; no external I/O."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from module.shared.context.types.messages import MessageRole

if TYPE_CHECKING:
    from module.shared.context.types.references import InheritedContextRef


class ConversationState(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class SessionState(StrEnum):
    OPEN = "open"
    WAITING_HANDOFF = "waiting_handoff" #完整轮后达到95%，停止向旧窗口创建新轮
    SEALED = "sealed"


class TurnState(StrEnum):
    CURRENT = "current"
    COMPLETE = "complete"


@dataclass(slots=True)
class Conversation:
    """用户可见的长期对话容器"""
    conversation_id: str #对话id
    conversation_title: str #对话容器名称
    character_id: str #角色id
    started_at: datetime #开始时间
    ended_at: datetime | None = None #结束时间
    state: ConversationState = ConversationState.ACTIVE
    current_session_id: str | None = None #当前会话id

    @classmethod
    def start(cls, conversation_id: str, character_id: str, started_at: datetime) -> "Conversation":
        """首条消息创建对话：空标题、active、尚未指向任何session"""
        return cls(conversation_id=conversation_id, conversation_title="",
                   character_id=character_id, started_at=started_at)

@dataclass(slots=True)
class Session:
    """会话"""
    session_id: str #会话id
    conversation_id: str #对话id
    state: SessionState #状态
    progress_tokens: int #进度token数
    started_at: datetime #开始时间
    ended_at: datetime | None = None #结束时间
    predecessor_session_id: str | None = None  # 前驱会话id
    inherited_context: InheritedContextRef | None = None #交接事务固定的摘要及连续尾部引用
    topic_jobs_registered_at: datetime | None = None  # 本session的object首次整合任务已完整登记
    session_seq: int = 1  # conversation 内的持久顺序，从 1 开始

    @classmethod
    def open(cls, session_id: str, conversation_id: str, started_at: datetime) -> "Session":
        """新对话的初始会话：open、无token消耗、无前驱"""
        return cls(session_id=session_id, conversation_id=conversation_id,
                   state=SessionState.OPEN, progress_tokens=0, started_at=started_at)


@dataclass(slots=True)
class Turn:
    """轮次"""
    turn_id: str #轮次id
    session_id: str #会话id
    turn_seq: int #轮次序号
    input_version: int #输入版本
    state: TurnState #状态
    accepted_message_id: str | None = None #接受的消息id
    active_attempt_id: str | None = None #激活的尝试id
    active_token: int = 0 #回复采用资格版本，不是租约

    @classmethod
    def create(cls, turn_id: str, session_id: str, turn_seq: int, input_version: int, state: TurnState) -> "Turn":
        """创建轮次；turn_seq、input_version和状态由调用方按当前路由决定"""
        return cls(turn_id=turn_id, session_id=session_id, turn_seq=turn_seq,
                   input_version=input_version, state=state)


@dataclass(slots=True)
class Message:
    """消息"""
    message_id: str #消息id
    turn_id: str #轮次id
    message_seq: int #消息序号
    role: MessageRole #角色
    content: str #内容
    action: str #ai的回复中包含动作，action用来接收此动作
    request_id: str | None #请求id
    created_at: datetime #创建时间

    @classmethod
    def create_user(cls, message_id: str, turn_id: str, message_seq: int, request_id: str,
                    content: str, created_at: datetime) -> "Message":
        """用户消息：无动作；message_seq由T1事务按turn内顺序分配"""
        return cls(message_id=message_id, turn_id=turn_id, message_seq=message_seq,
                   role=MessageRole.USER, content=content, action="",
                   request_id=request_id, created_at=created_at)
