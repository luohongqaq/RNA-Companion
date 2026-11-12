"""会话生命周期用例。"""
from logger import stage

import base64
import binascii
import json
from datetime import datetime, timezone

from module.chat.conversation.core.domain.ports import ConversationRepository
from module.chat.conversation.core.domain.contracts import ConversationDetails, CreateConversation
from module.chat.conversation.core.domain.contracts import UpdateConversation
from module.chat.conversation.core.domain.models import ConversationState
from module.shared.common.errors.base import InvalidInputError, InvalidStateError, NotFoundError
from module.shared.common.types.pagination import Page
from module.shared.common.ids import IdGenerator


class ConversationService:
    def __init__(self, repository: ConversationRepository) -> None:
        self._repository = repository

    @stage("conversation.create")
    async def create(
        self, *, character_id: str, title: str | None = None, with_session: bool = False,
    ) -> ConversationDetails:
        """创建一个新会话。"""
        character_id = character_id.strip()
        title = title.strip() if title is not None else None
        if not 1 <= len(character_id) <= 128:
            raise InvalidStateError("角色 ID 必须为 1～128 个字符")
        if title is not None and not 1 <= len(title) <= 200:
            raise InvalidStateError("标题必须为 1～200 个字符")
        return await self._repository.create_conversation(
            CreateConversation(IdGenerator.new("conv"), character_id, title, with_session)
        )

    async def get(self, conversation_id: str) -> ConversationDetails:
        """获取会话详情。"""
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise InvalidStateError("会话 ID 不能为空")
        result = await self._repository.get_conversation(conversation_id)
        if result is None:
            raise NotFoundError("会话不存在")
        return result

    async def list(
        self, *, limit: int = 20, cursor: str | None = None,
        state: str | None = None, character_id: str | None = None,
    ) -> Page[ConversationDetails]:
        """查询会话摘要；游标绑定筛选条件，多取一条判断是否还有下一页。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInputError("limit 必须为 1～100")
        if state not in (None, "active", "archived"):
            raise InvalidInputError("会话状态无效")
        if character_id is not None and (
            not isinstance(character_id, str) or not character_id.strip()
            or len(character_id) > 128
        ):
            raise InvalidInputError("角色 ID 必须为 1～128 个字符")
        before = None
        # 解码cursor并检查
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
                    raise ValueError
                raw = base64.b64decode(
                    cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True,
                )
                data = json.loads(raw)
                if (not isinstance(data, dict) or data.get("v") != 1
                        or data.get("state") != state or data.get("character_id") != character_id
                        or not isinstance(data.get("id"), str) or not data["id"]
                        or len(data["id"]) > 128):
                    raise ValueError
                timestamp = datetime.fromisoformat(data["time"])
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError
                before = (timestamp.astimezone(timezone.utc), data["id"])
            except (ValueError, TypeError, KeyError, binascii.Error, OverflowError) as exc:
                raise InvalidInputError("游标无效或筛选条件已改变，请从第一页查询") from exc
        # 查询
        rows = await self._repository.list_conversations(
            limit=limit + 1, state=state, character_id=character_id, before=before,
        )
        # 看一下还有没有下一页，有就编码游标，没有就是none
        items = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            raw = json.dumps({
                "v": 1, "time": last.updated_at.isoformat(), "id": last.conversation_id,
                "state": state, "character_id": character_id,
            }, separators=(",", ":")).encode("utf-8")
            next_cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return Page(items=items, next_cursor=next_cursor)

    @stage("conversation.update")
    async def update(
        self, conversation_id: str, *, title: str | None = None,
        update_title: bool = False, state: str | None = None,
    ) -> ConversationDetails:
        """部分更新会话；归档只限制新输入，已提交任务继续执行。"""
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise InvalidInputError("会话 ID 不能为空")
        if not update_title and state is None:
            raise InvalidInputError("至少提供 title 或 state")
        if update_title and title is not None:
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
                raise InvalidInputError("标题必须为 1～200 个字符")
            title = title.strip()
        if state not in (None, "active", "archived"):
            raise InvalidInputError("会话状态无效")
        result = await self._repository.update_conversation(UpdateConversation(
            conversation_id=conversation_id, title=title, update_title=update_title,
            state=ConversationState(state) if state is not None else None,
        ))
        if result is None:
            raise NotFoundError("会话不存在")
        return result

    async def update_title(self, conversation_id: str, title: str) -> ConversationDetails:
        return await self.update(conversation_id, title=title, update_title=True)
