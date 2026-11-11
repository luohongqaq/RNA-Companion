"""Chat application services."""
from logger import stage
import base64
import binascii
import json
from dataclasses import asdict
from collections.abc import Callable
import logging

from module.shared.common.types.pagination import Page
from module.chat.conversation.core.domain.contracts import ConversationMessageItem
from module.chat.conversation.core.domain.contracts import AcceptedReply, SaveMessageInput
from module.chat.conversation.core.domain.ports import ChatRepository
from module.shared.common.errors.base import InvalidStateError, InvalidInputError, NotFoundError


class MessageService:
    def __init__(self, repository: ChatRepository, *, notify_generation: Callable[[], None] | None = None) -> None:
        self._repository = repository
        self._notify_generation = notify_generation

    @stage("chat.accept")
    async def accept_message(self, command: SaveMessageInput) -> dict[str, object]:
        """验证输入，等待 T1 提交，并保持现有 API 使用的字典返回格式。

        输入:
        SaveMessageInput: {request_id, conversation_id, character_id, content, client_created_at}
        输出:
        SaveMessageResult各字段组成的dict:
        SaveMessageResult: {request_id, message_id, turn_id, input_version, job_id,
                            save_state, generation_state}
        """
        try:
            command.validate()
        except ValueError as exc:
            raise InvalidStateError(str(exc)) from exc
        result = await self._repository.accept_message(command)
        logging.getLogger(__name__).info("用户消息已提交", extra={
            "event": "message_committed", "conversation_id": command.conversation_id,
            "message_request_id": command.request_id, "job_id": result.job_id,
        })
        # 仓储已成功退出T1事务；通知只是加速，失败后执行器仍可扫描持久任务。
        if self._notify_generation is not None:
            try:
                self._notify_generation()
            except Exception as exc:
                logging.getLogger(__name__).warning("生成通知未送达（%s）；持久任务保留", type(exc).__name__)
        return asdict(result)

    @stage("chat.reply.read")
    async def reply_message(self, job_id: str) -> AcceptedReply | None:
        """模型调用及T3提交后，从数据库读取正式消息并组装正文、视觉状态和标识。

        未正式采用的候选不返回；不依赖执行器内存中的模型结果。
        """
        if not isinstance(job_id, str) or not job_id.strip():
            raise InvalidStateError("job_id不能为空")
        return await self._repository.read_accepted_reply(job_id)


    async def list_messages(
        self, *, conversation_id: str, limit: int = 30, cursor: str | None = None,
    ) -> Page[ConversationMessageItem]:
        """读取最近历史，向前翻页；返回页内顺序为从早到晚。"""
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise InvalidInputError("会话 ID 不能为空")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInputError("limit 必须为 1～100")
        before = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
                    raise ValueError
                data = json.loads(base64.b64decode(
                    cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True,
                ))
                if (not isinstance(data, dict) or data.get("v") != 1
                        or data.get("conversation_id") != conversation_id):
                    raise ValueError
                key = data["before"]
                if (not isinstance(key, dict)
                        or set(key) != {"session_seq", "session_id", "turn_seq", "message_seq"}
                        or type(key["session_seq"]) is not int
                        or not 1 <= key["session_seq"] < 2**63
                        or not isinstance(key["session_id"], str)
                        or not 1 <= len(key["session_id"]) <= 128
                        or any(type(key[name]) is not int or not 1 <= key[name] < 2**63
                               for name in ("turn_seq", "message_seq"))):
                    raise ValueError
                before = (key["session_seq"], key["session_id"], key["turn_seq"], key["message_seq"])
            except (ValueError, TypeError, KeyError, binascii.Error) as exc:
                raise InvalidInputError("消息游标无效或不属于当前会话") from exc
        rows = await self._repository.list_conversation_messages(
            conversation_id=conversation_id, limit=limit + 1, before=before,
        )
        if rows is None:
            raise NotFoundError("会话不存在")
        items = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            oldest = items[-1]
            data = {"v": 1, "conversation_id": conversation_id, "before": {
                "session_seq": oldest.session_seq,
                "session_id": oldest.session_id,
                "turn_seq": oldest.turn_seq,
                "message_seq": oldest.message_seq,
            }}
            raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
            next_cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return Page(items=tuple(reversed(items)), next_cursor=next_cursor)
