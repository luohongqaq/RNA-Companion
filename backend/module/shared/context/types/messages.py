"""聊天、压缩和记忆共用的消息与完整轮读取结果。"""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from module.shared.context.types.visual import VisualState


class MessageRole(StrEnum):
    USER = "user"
    CHARACTER = "character"


@dataclass(frozen=True, slots=True)
class MessageListItem:
    """消息列表项"""
    message_id: str #消息id
    turn_id: str #轮次id
    message_seq: int #消息序号
    role: MessageRole #角色
    content: str #内容
    visual: VisualState | None #视觉状态
    created_at: datetime #创建时间


@dataclass(frozen=True, slots=True)
class CompletedTurnResult:
    """完整轮(一轮)读取结果，供聊天历史、上下文压缩和交接共用。
    user_messages按message_seq排序；accepted_reply是该轮正式采用的角色消息。
    消息ID、正文、时间和视觉状态统一由MessageListItem提供。
    这是查询结果，不替代负责状态迁移的Turn实体；来源修订清单由上下文模块记录。
    """

    turn_id: str
    session_id: str
    turn_seq: int
    user_messages: tuple[MessageListItem, ...]
    accepted_reply: MessageListItem


@dataclass(frozen=True, slots=True)
class PromptMessage:
    """提示词消息"""
    role: Literal["system", "developer", "user", "assistant"] #模型输入角色，与聊天消息的character角色区分
    content: str #消息内容

    @classmethod
    def reference(cls, label: str, text: str) -> "PromptMessage":
        return cls("user", f"【参考资料：{label}】\n{text}\n【参考资料结束】")

    @classmethod
    def from_turns(cls, turns: tuple[CompletedTurnResult, ...]) -> tuple["PromptMessage", ...]:
        messages = []
        for turn in turns:
            messages.extend(cls("user", message.content) for message in turn.user_messages)
            messages.append(cls("assistant", turn.accepted_reply.content))
        return tuple(messages)
