"""会话、消息、事件与保存回执的 SQLite 映射。"""

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from module.shared.context.types.visual import Expression, Pose, VisualState
from module.chat.conversation.core.domain.contracts import (
    AcceptedReply,
    ConversationDetails,
    RequestReceiptResult,
    SaveMessageResult,
)
from module.shared.context.types.messages import CompletedTurnResult, MessageListItem
from module.chat.conversation.core.domain.events import (
    ChatEvent,
    ReplyCommittedEvent,
    GenerationFailedEvent,
    GenerationCancelledEvent,
)
from module.chat.conversation.core.domain.models import (
    Conversation,
    ConversationState,
    Message,
    Session,
    SessionState,
    Turn,
    TurnState,
)
from module.shared.context.types.messages import MessageRole
from module.shared.common.errors.base import InvalidStateError
from module.chat.reply.core.domain.models import GenerationFailure, GenerationFailureKind
from storage.sqlite.models import (
    ChatEventRow,
    ConversationRow,
    MessageRow,
    RequestReceiptRow,
    SessionRow,
    TurnRow,
)
from storage.sqlite.value_codecs import SQLiteValueCodec


def conversation_to_row(value: Conversation, *, updated_at: datetime) -> ConversationRow:
    """把对话业务对象转成新行，仅用于插入；updated_at由调用方按事务时刻单独传入。"""
    return ConversationRow(conversation_id=value.conversation_id, title=value.conversation_title,
        character_id=value.character_id, state=value.state.value, current_session_id=value.current_session_id,
        created_at=value.started_at, updated_at=updated_at, ended_at=value.ended_at)


def conversation_to_domain(row: ConversationRow) -> Conversation:
    """把对话行还原成业务对象。"""
    return Conversation(row.conversation_id, row.title or "", row.character_id, row.created_at,
                        row.ended_at, ConversationState(row.state), row.current_session_id)


def conversation_to_details(row: ConversationRow) -> ConversationDetails:
    """把对话行转成对外展示的详情对象。"""
    return ConversationDetails(row.conversation_id, row.character_id, row.title,
        ConversationState(row.state), row.current_session_id, row.created_at, row.updated_at)


def session_to_row(value: Session, *, auto_successor_of: str | None = None) -> SessionRow:
    """把会话业务对象转成新行，仅用于插入；auto_successor_of由调用方按自动接续关系传入。"""
    return SessionRow(session_id=value.session_id, conversation_id=value.conversation_id,
        session_seq=value.session_seq,
        session_state=value.state.value, session_progress_tokens=value.progress_tokens,
        session_start_time=value.started_at, session_end_time=value.ended_at,
        predecessor_session_id=value.predecessor_session_id, auto_successor_of=auto_successor_of,
        inherited_context_json=SQLiteValueCodec.inherited_context_to_json(value.inherited_context),
        topic_jobs_registered_at=value.topic_jobs_registered_at)


def session_to_domain(row: SessionRow) -> Session:
    """把会话行还原成业务对象。"""
    return Session(row.session_id, row.conversation_id, SessionState(row.session_state),
        row.session_progress_tokens, row.session_start_time, row.session_end_time, row.predecessor_session_id,
        SQLiteValueCodec.inherited_context_from_json(row.inherited_context_json), row.topic_jobs_registered_at, row.session_seq)


def turn_to_row(value: Turn, *, row_version: int = 0, turn_token_count: int = 0,
                cumulative_token_count: int = 0, tokenizer_version: str | None = None) -> TurnRow:
    """把轮次业务对象转成新行，仅用于插入；行版本与token统计由存储层另行维护。"""
    return TurnRow(turn_id=value.turn_id, session_id=value.session_id, turn_seq=value.turn_seq,
        input_version=value.input_version, turn_state=value.state.value,
        accepted_message_id=value.accepted_message_id, active_attempt_id=value.active_attempt_id,
        active_token=value.active_token, row_version=row_version, turn_token_count=turn_token_count,
        cumulative_token_count=cumulative_token_count, tokenizer_version=tokenizer_version)


def turn_to_domain(row: TurnRow) -> Turn:
    """把轮次行还原成业务对象，丢弃行版本与token统计列。"""
    return Turn(row.turn_id, row.session_id, row.turn_seq, row.input_version, TurnState(row.turn_state),
                row.accepted_message_id, row.active_attempt_id, row.active_token)


def completed_turn_to_result(row: TurnRow, messages: Sequence[MessageRow]) -> CompletedTurnResult:
    """只选该完整轮的用户输入及正式采用回复，不包含未采用的角色消息。"""
    # 验证turn的状态是否已经完成、是否已经有了ai回复
    if row.turn_state != TurnState.COMPLETE.value or row.accepted_message_id is None:
        raise InvalidStateError(f"turn has no accepted reply: {row.turn_id}")
    # 验证turn_id有没有串
    if any(message.turn_id != row.turn_id for message in messages):
        raise InvalidStateError(f"message belongs to another turn: {row.turn_id}")
    # 提取ai回复的那条消息
    accepted = next((message for message in messages if message.message_id == row.accepted_message_id), None)
    # 验证对应的角色对不对
    if accepted is None or accepted.role != MessageRole.CHARACTER.value:
        raise InvalidStateError(f"accepted reply is missing or invalid: {row.turn_id}")
    # 提取用户消息
    users = tuple(message_to_list_item(message) for message in sorted(messages, key=lambda item: item.message_seq)
                  if message.role == MessageRole.USER.value)
    if not users:
        raise InvalidStateError(f"complete turn has no user messages: {row.turn_id}")
    return CompletedTurnResult(row.turn_id, row.session_id, row.turn_seq, users, message_to_list_item(accepted))


def message_to_row(value: Message, *, client_created_at: datetime | None = None,
                   visual: VisualState | None = None) -> MessageRow:
    """把消息业务对象转成新行；action原样保存，不隐式解释为视觉状态。"""
    # action is preserved as-is; it is not implicitly interpreted as a VisualState.
    return MessageRow(message_id=value.message_id, turn_id=value.turn_id, message_seq=value.message_seq,
        role=value.role.value, content=value.content, action=value.action, request_id=value.request_id,
        created_at=value.created_at, client_created_at=client_created_at, visual_json=visual_to_json(visual))


def message_to_domain(row: MessageRow) -> Message:
    """把消息行还原成业务对象，丢弃visual_json与client_created_at。"""
    return Message(row.message_id, row.turn_id, row.message_seq, MessageRole(row.role),
                   row.content, row.action, row.request_id, row.created_at)


def accepted_reply_to_json(value: AcceptedReply) -> dict[str, Any]:
    """将正式回复转换为可持久化的事件快照，时间统一为 UTC。"""
    if value.created_at.tzinfo is None or value.created_at.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return {
        "conversation_id": value.conversation_id,
        "turn_id": value.turn_id,
        "message_id": value.message_id,
        "content": value.content,
        "visual": visual_to_json(value.visual),
        "created_at": value.created_at.astimezone(timezone.utc).isoformat(),
    }


def chat_event_to_domain(row: ChatEventRow) -> ChatEvent:
    """还原回复、失败或取消事件的快照，不依赖后续消息或任务的状态。"""
    try:
        if row.event_type in ("generation_failed", "generation_cancelled"):
            data = row.failure_json if row.event_type == "generation_failed" else row.cancel_json
            if (not isinstance(data, dict) or data["conversation_id"] != row.conversation_id
                    or type(data["turn_id"]) is not str or not data["turn_id"]
                    or type(data["input_version"]) is not int or data["input_version"] < 1
                    or type(data["manual_retry_count"]) is not int or data["manual_retry_count"] < 0
                    or data["manual_retry_count"] != row.manual_retry_count):
                raise ValueError("任务状态事件快照不一致")
            fields = (row.event_id, row.created_at, row.job_id, row.conversation_id,
                      data["turn_id"], data["input_version"], row.manual_retry_count)
            if row.event_type == "generation_cancelled":
                return GenerationCancelledEvent(*fields)
            error = failure_from_json(data["error"])
            if (error is None or type(error.retryable) is not bool
                    or not isinstance(error.code, str) or not error.code
                    or not isinstance(error.message, str) or not error.message):
                raise ValueError("失败事件快照不一致")
            return GenerationFailedEvent(*fields, error)
        data = row.reply_json
        if (row.event_type != "reply_committed" or not isinstance(data, dict)
                or data["conversation_id"] != row.conversation_id
                or data["message_id"] != row.message_id
                or not isinstance(data["turn_id"], str) or not data["turn_id"]
                or not isinstance(data["content"], str) or not data["content"].strip()):
            raise ValueError("事件类型或回复标识不一致")
        reply = AcceptedReply(
            conversation_id=data["conversation_id"], turn_id=data["turn_id"],
            message_id=data["message_id"], content=data["content"],
            visual=visual_from_json(data["visual"]), created_at=SQLiteValueCodec.datetime_from_json(data["created_at"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidStateError(f"聊天事件回复快照损坏：{row.event_id}") from exc
    return ReplyCommittedEvent(row.event_id, row.created_at, row.job_id, reply)


def message_to_list_item(row: MessageRow) -> MessageListItem:
    """把消息行转成列表项，visual由visual_json还原，丢弃action与request_id。"""
    visual = visual_from_json(row.visual_json)
    return MessageListItem(row.message_id, row.turn_id, row.message_seq,
                           MessageRole(row.role), row.content, visual, row.created_at)


def receipt_to_row(value: RequestReceiptResult, *, created_at: datetime) -> RequestReceiptRow:
    """把保存回执转成新行；结果同时落在独立列和result_json里，便于按列查询与整体校验。"""
    if value.operation != "send_message" or value.request_id != value.result.request_id:
        raise ValueError("send_message receipt must match its result request_id")
    result = value.result
    return RequestReceiptRow(request_id=value.request_id, operation=value.operation, payload_hash=value.payload_hash,
        message_id=result.message_id, turn_id=result.turn_id, input_version=result.input_version,
        job_id=result.job_id, receipt_state=result.save_state,
        result_json={
            "request_id": result.request_id,
            "message_id": result.message_id,
            "turn_id": result.turn_id,
            "input_version": result.input_version,
            "job_id": result.job_id,
            "save_state": result.save_state,
            "generation_state": result.generation_state,
        }, created_at=created_at)


def receipt_to_domain(row: RequestReceiptRow) -> RequestReceiptResult:
    """把回执行还原成业务对象；独立列与result_json不一致直接报错。"""
    if row.operation != "send_message":
        raise ValueError("unsupported receipt operation")
    data = row.result_json
    result = SaveMessageResult(
        request_id=data["request_id"],
        message_id=data["message_id"],
        turn_id=data["turn_id"],
        input_version=data["input_version"],
        job_id=data["job_id"],
        save_state=data["save_state"],
        generation_state=data["generation_state"],
    )
    if (row.request_id, row.message_id, row.turn_id, row.input_version, row.job_id, row.receipt_state) != (
        result.request_id, result.message_id, result.turn_id, result.input_version, result.job_id, result.save_state,
    ):
        raise ValueError("receipt columns do not match its saved result")
    return RequestReceiptResult(row.operation, row.request_id, row.payload_hash, result)


def visual_to_json(value: VisualState | None) -> dict[str, Any] | None:
    """把视觉状态转成JSON对象。"""
    if value is None:
        return None
    return {
        "pose_name": {"pose_id": value.pose_name.pose_id, "pose_name": value.pose_name.pose_name},
        "expression_name": {
            "expression_id": value.expression_name.expression_id,
            "expression_name": value.expression_name.expression_name,
        },
    }


def visual_from_json(data: dict[str, Any] | None) -> VisualState | None:
    """把JSON对象还原成视觉状态。"""
    if data is None:
        return None
    return VisualState(
        pose_name=Pose(pose_id=data["pose_name"]["pose_id"], pose_name=data["pose_name"]["pose_name"]),
        expression_name=Expression(
            expression_id=data["expression_name"]["expression_id"],
            expression_name=data["expression_name"]["expression_name"],
        ),
    )


def failure_to_json(value: GenerationFailure | None) -> dict[str, Any] | None:
    """把生成失败对象转成JSON对象，时间转ISO文本。"""
    if value is None:
        return None
    return {
        "kind": value.kind.value, "code": value.code, "message": value.message,
        "retryable": value.retryable, "occurred_at": SQLiteValueCodec.datetime_to_json(value.occurred_at),
    }


def failure_from_json(data: dict[str, Any] | None) -> GenerationFailure | None:
    """把JSON对象还原成生成失败对象。"""
    if data is None:
        return None
    return GenerationFailure(
        kind=GenerationFailureKind(data["kind"]), code=data["code"], message=data["message"],
        retryable=data["retryable"], occurred_at=SQLiteValueCodec.datetime_from_json(data["occurred_at"]),
    )
