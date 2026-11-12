"""声明存储接口"""

from typing import Protocol
from datetime import datetime

from module.chat.conversation.core.domain.contracts import AcceptedReply, SaveMessageInput, SaveMessageResult
from module.chat.conversation.core.domain.contracts import ConversationDetails, CreateConversation
from module.chat.conversation.core.domain.contracts import ConversationMessageItem, MessageCursor
from module.chat.conversation.core.domain.contracts import UpdateConversation
from module.chat.conversation.core.domain.contracts import SealSession, SealSessionResult
from module.chat.conversation.core.domain.events import ChatEvent
from module.chat.conversation.core.domain.models import Session


class ConversationRepository(Protocol):
    async def update_conversation(self, command: UpdateConversation) -> ConversationDetails | None:
        """原子修改指定字段；不存在返回 None，相同值不重复更新时间。"""
        ...

    async def list_conversations(
        self, *, limit: int, state: str | None, character_id: str | None,
        before: tuple[datetime, str] | None,
    ) -> tuple[ConversationDetails, ...]:
        """按 updated_at、conversation_id 双倒序读取，排除游标边界本身。"""
        ...

    async def get_conversation(self, conversation_id: str) -> ConversationDetails | None:
        """读取会话元数据，不存在时返回 None。"""
        ...

    async def create_conversation(self, command: CreateConversation) -> ConversationDetails:
        """校验角色并原子创建空会话；提交成功后返回。"""
        ...

    async def seal_target(self, conversation_id: str, session_id: str) -> int:
        """核对可封存并返回最后一轮序号；只读，不改状态。"""
        ...

    async def mark_waiting_handoff(self, conversation_id: str, session_id: str) -> None:
        """把当前会话置为等待交接；已是该状态时不重复写。"""
        ...

    async def get_current_session_id(self, conversation_id: str) -> str | None:
        """读取对话当前会话；等待交接时用来判断是否已经换新。"""
        ...

    async def release_handoff(self, conversation_id: str, session_id: str) -> None:
        """摘要失败时把会话退回可继续对话的状态；不满足条件时什么都不做。"""
        ...


class ChatRepository(Protocol):
    async def list_events_after(
        self, event_id: int, *, limit: int = 100,
    ) -> tuple[ChatEvent, ...]:
        """按 event_id 升序读取严格大于游标的事件；游标无需对应现存记录。"""
        ...

    async def list_conversation_messages(
        self, *, conversation_id: str, limit: int, before: MessageCursor | None,
    ) -> tuple[ConversationMessageItem, ...] | None:
        """倒序查询正式历史；会话不存在返回 None，空会话返回空元组。"""
        ...

    async def read_accepted_reply(self, job_id: str) -> AcceptedReply | None:
        """从messages读取该任务正式采用的回复；尚未采用返回None。"""
        ...

    async def accept_message(self, command: SaveMessageInput) -> SaveMessageResult:
        """Atomically execute the T1 message acceptance transaction."""
        ...


class ContextHandoffPort(Protocol):
    async def prepare_handoff(self, session_id: str, through_turn_seq: int) -> str:
        """登记交接摘要任务；同一会话已有任务时沿用原任务。"""
        ...

    async def commit_handoff(self, session_id: str) -> Session | None:
        """摘要就绪时完成交接并返回接替会话；尚未就绪返回 None。"""
        ...

    async def commit_degraded_handoff(self, session_id: str) -> Session | None:
        """摘要反复失败时的兜底交接：放弃摘要，只把冻结点之后的原文带给后继会话；
        冻结点之后没有可带原文、或尾部放不进新窗口时返回 None。"""
        ...

    async def handoff_status(self, session_id: str) -> str | None:
        """读取交接任务状态；失败时用例要回滚，不让对话卡住。"""
        ...
