"""声明存储接口"""

from typing import Protocol
from datetime import datetime

from module.chat.conversation.core.domain.contracts import AcceptedReply, SaveMessageInput, SaveMessageResult
from module.chat.conversation.core.domain.contracts import ConversationDetails, CreateConversation
from module.chat.conversation.core.domain.contracts import ConversationMessageItem, MessageCursor
from module.chat.conversation.core.domain.contracts import UpdateConversation
from module.chat.conversation.core.domain.events import ChatEvent


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

