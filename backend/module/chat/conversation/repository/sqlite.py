"""会话元数据、消息回执和聊天事件的 SQLite 仓储。"""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from module.chat.conversation.core.domain.contracts import AcceptedReply, RequestReceiptResult, SaveMessageInput, SaveMessageResult
from module.chat.conversation.core.domain.contracts import (
    ConversationDetails, ConversationMessageItem, CreateConversation, MessageCursor, UpdateConversation,
)
from module.chat.conversation.core.domain.events import ChatEvent
from module.shared.context.types.messages import MessageRole
from module.chat.conversation.core.domain.models import Conversation, ConversationState, Message, Session, SessionState, Turn, TurnState
from module.shared.common.errors.base import IdempotencyConflictError, InvalidStateError, NotFoundError
from module.shared.common.ids import IdGenerator
from module.chat.reply.core.domain.models import AttemptState, GenerationJob, GenerationJobStatus
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.reply.repository import mappers as reply_mappers
from module.chat.conversation.repository import mappers as conversation_mappers
from storage.sqlite.models import (
    CharacterRow, ConversationRow, GenerationAttemptRow, GenerationJobRow,
    MessageRow, RequestReceiptRow, SessionRow, TurnRow, ChatEventRow,
)



class SQLiteChatRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def update_conversation(self, command: UpdateConversation) -> ConversationDetails | None:
        """更新conversation"""
        async with self._database.transaction() as session:
            row = await session.get(ConversationRow, command.conversation_id)
            if row is None:
                return None
            now = datetime.now(timezone.utc)
            changed = False
            if command.update_title and row.title != command.title:
                row.title = command.title
                changed = True
            if command.state is not None and row.state != command.state.value:
                row.state = command.state.value
                row.ended_at = now if row.state == "archived" else None
                changed = True
            if changed:
                row.updated_at = now
                await session.flush()
            result = conversation_mappers.conversation_to_details(row)
        return result

    async def create_conversation(self, command: CreateConversation) -> ConversationDetails:
        """创建conversation"""
        async with self._database.transaction() as session:
            if await session.get(CharacterRow, command.character_id) is None:
                raise NotFoundError("角色不存在")
            now = datetime.now(timezone.utc)
            row = ConversationRow(
                conversation_id=command.conversation_id,
                character_id=command.character_id,
                current_session_id=None,
                title=command.title,
                state="active",
                created_at=now,
                updated_at=now,
                ended_at=None,
            )
            session.add(row)
            await session.flush()
            result = conversation_mappers.conversation_to_details(row)
        return result

    async def get_conversation(self, conversation_id: str) -> ConversationDetails | None:
        """获取指定角色的 conversation"""
        async with self._database.db_session() as session:
            row = await session.get(ConversationRow, conversation_id)
            return conversation_mappers.conversation_to_details(row) if row is not None else None

    async def list_conversations(
        self, *, limit: int, state: str | None, character_id: str | None,
        before: tuple[datetime, str] | None,
    ) -> tuple[ConversationDetails, ...]:
        """根据指定角色列出conversations"""
        stmt = select(ConversationRow)
        if state is not None:
            stmt = stmt.where(ConversationRow.state == state)
        if character_id is not None:
            stmt = stmt.where(ConversationRow.character_id == character_id)
        if before is not None:
            timestamp, conversation_id = before
            # 查出所有“更新时间更早，或者更新时间相同但 ID 更小”的记录
            stmt = stmt.where(or_(
                ConversationRow.updated_at < timestamp,
                and_(ConversationRow.updated_at == timestamp,
                     ConversationRow.conversation_id < conversation_id),
            ))
        # 倒序排序：一般来说是1-10（最早是1），所以返回的顺序是10-1
        stmt = stmt.order_by(
            ConversationRow.updated_at.desc(), ConversationRow.conversation_id.desc(),
        ).limit(limit)
        async with self._database.db_session() as session:
            rows = (await session.scalars(stmt)).all()
            return tuple(conversation_mappers.conversation_to_details(row) for row in rows)

    async def read_accepted_reply(self, job_id: str) -> AcceptedReply | None:
        """在一致读快照中按任务定位正式messages记录，不使用candidate_json作为返回正文。"""
        async with self._database.db_session() as db:
            job = await db.get(GenerationJobRow, job_id)
            if job is None:
                raise NotFoundError("生成任务不存在")
            if job.state != GenerationJobStatus.DONE.value:
                return None
            turn = await db.get(TurnRow, job.turn_id)
            if turn is None or turn.turn_state != TurnState.COMPLETE.value or turn.accepted_message_id is None:
                raise InvalidStateError("已完成任务缺少正式回复指针")
            session = await db.get(SessionRow, turn.session_id)
            message = await db.get(MessageRow, turn.accepted_message_id)
            attempt = await db.get(GenerationAttemptRow, job.current_attempt_id) if job.current_attempt_id else None
            if (session is None or message is None or message.turn_id != turn.turn_id or message.role != 'character'
                    or attempt is None or attempt.attempt_state != AttemptState.ACCEPTED.value
                    or attempt.job_id != job_id or attempt.turn_id != turn.turn_id):
                raise InvalidStateError("正式回复记录或采用关系不一致")
            return AcceptedReply(session.conversation_id, turn.turn_id, message.message_id,
                message.content, conversation_mappers.visual_from_json(message.visual_json), message.created_at)

    async def accept_message(self, command: SaveMessageInput) -> SaveMessageResult:
        """T1入口：同一request_id重复提交时按payload_hash校验后原样重放回执；
        否则按对话是否存在分流到首条消息或追加消息，全部在一个短事务内完成。

        输入:
        SaveMessageInput: {request_id, conversation_id, character_id, content, client_created_at}
        输出:
        SaveMessageResult: {request_id, message_id, turn_id, input_version, job_id,
                            save_state, generation_state}
        """
        # 计算hash,避免同一request_id被重复处理
        payload_hash = self._message_payload_hash(command)
        # 开启消息接收短事务
        async with self._database.transaction() as db_session:
            # 检查是否已经处理过这个请求：网络异常时前端可能会重试导致发送了重复请求
            receipt_row = await db_session.get(RequestReceiptRow, (command.request_id, "send_message"))
            if receipt_row is not None:
                #非空，检查hash是否相同,不相同说明一个request_id对应了不同payload
                if receipt_row.payload_hash != payload_hash:
                    raise IdempotencyConflictError("request_id was already used with a different payload")
                # hash相同,说明已经处理过了,直接调用构造方法返回
                result = conversation_mappers.receipt_to_domain(receipt_row).result
            else:
                # 分流：对话不存在走首条消息路由，已存在走追加路由
                if await db_session.get(ConversationRow, command.conversation_id) is None:
                    # 新对话必须指向已存在的角色
                    if await db_session.get(CharacterRow, command.character_id) is None:
                        raise NotFoundError("character does not exist")
                    result = await self._create_first_message(db_session, command, payload_hash)
                else:
                    result = await self._add_user_message(db_session, command, payload_hash)
        # Leaving transaction() must successfully commit before acknowledging the message.
        return result

    async def list_conversation_messages(
        self, *, conversation_id: str, limit: int, before: MessageCursor | None,
    ) -> tuple[ConversationMessageItem, ...] | None:
        keys = (SessionRow.session_seq, SessionRow.session_id, TurnRow.turn_seq, MessageRow.message_seq)
        stmt = select(MessageRow, SessionRow.session_seq, SessionRow.session_id, TurnRow.turn_seq).join(
            TurnRow, MessageRow.turn_id == TurnRow.turn_id,
        ).join(SessionRow, TurnRow.session_id == SessionRow.session_id).where(
            SessionRow.conversation_id == conversation_id,
            or_(MessageRow.role == "user",
                and_(MessageRow.role == "character", TurnRow.turn_state == "complete",
                     TurnRow.accepted_message_id == MessageRow.message_id)),
        )
        if before is not None:
            stmt = stmt.where(tuple_(*keys) < tuple_(*before))
        stmt = stmt.order_by(*(key.desc() for key in keys)).limit(limit)
        async with self._database.db_session() as db:
            if await db.get(ConversationRow, conversation_id) is None:
                return None
            rows = (await db.execute(stmt)).all()
            return tuple(ConversationMessageItem(
                message_id=row.message_id, turn_id=row.turn_id, message_seq=row.message_seq,
                role=MessageRole(row.role), content=row.content,
                visual=conversation_mappers.visual_from_json(row.visual_json), created_at=row.created_at,
                session_id=session_id, session_seq=order, turn_seq=turn_seq,
            ) for row, order, session_id, turn_seq in rows)

    async def list_events_after(
            self, event_id: int, *, limit: int = 100,
    ) -> tuple[ChatEvent, ...]:
        """按事件序号升序读取，供 SSE 推送和断线补发。"""
        if type(event_id) is not int or not 0 <= event_id < 2**63:
            raise ValueError("event_id 必须为非负 SQLite 整数")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit 必须为 1～100")
        async with self._database.db_session() as db:
            rows = (await db.scalars(select(ChatEventRow).where(
                ChatEventRow.event_id > event_id,
            ).order_by(ChatEventRow.event_id.asc()).limit(limit))).all()
            return tuple(conversation_mappers.chat_event_to_domain(row) for row in rows)



    async def _create_first_message(
        self, db_session: AsyncSession, command: SaveMessageInput, payload_hash: str,
    ) -> SaveMessageResult:
        """第一条消息创建时需要同步创建conversation、session、turn、message

        输入:
        SaveMessageInput: {request_id, conversation_id, character_id, content, client_created_at}
        payload_hash
        输出:
        SaveMessageResult: {request_id, message_id, turn_id, input_version, job_id,
                            save_state, generation_state}
        """
        now = datetime.now(timezone.utc)
        session_id, turn_id = IdGenerator.new("session"), IdGenerator.new("turn")
        message_id, job_id = IdGenerator.new("msg"), IdGenerator.new("job")

        # 创建conversation
        conversation = Conversation.start(command.conversation_id, command.character_id, now)
        conversation_row = conversation_mappers.conversation_to_row(conversation, updated_at=now)
        db_session.add(conversation_row)
        await db_session.flush()

        # 创建session
        session = Session.open(session_id, command.conversation_id, now)
        db_session.add(conversation_mappers.session_to_row(session))
        await db_session.flush()
        conversation_row.current_session_id = session_id

        # 创建turn
        turn = Turn.create(turn_id, session_id, 1, 1, TurnState.CURRENT)
        db_session.add(conversation_mappers.turn_to_row(turn))
        await db_session.flush()

        # 创建message
        message = Message.create_user(message_id, turn_id, 1, command.request_id, command.content, now)
        db_session.add(conversation_mappers.message_to_row(message, client_created_at=command.client_created_at))
        await db_session.flush()

        # 创建generation_job: 生成回复任务
        job = GenerationJob.register_generate_reply(job_id, turn_id, 1, now)
        db_session.add(reply_mappers.generation_job_to_row(job))
        await db_session.flush()

        result = SaveMessageResult(command.request_id, message_id, turn_id, 1, job_id)
        receipt = RequestReceiptResult("send_message", command.request_id, payload_hash, result)
        db_session.add(conversation_mappers.receipt_to_row(receipt, created_at=now))
        await db_session.flush()
        return result

    async def _add_user_message(
        self, db_session: AsyncSession, command: SaveMessageInput, payload_hash: str,
    ) -> SaveMessageResult:
        """追加用户消息：加入当前turn或开启新turn；不再创建conversation和session

        输入:
        SaveMessageInput: {request_id, conversation_id, character_id, content, client_created_at}
        payload_hash
        输出:
        SaveMessageResult: {request_id, message_id, turn_id, input_version, job_id,
                            save_state, generation_state}
        """
        now = datetime.now(timezone.utc)
        message_id, job_id = IdGenerator.new("msg"), IdGenerator.new("job")

        # 读取当前聊天路由 conversation → current_session → current_turn
        conversation_row = await db_session.get(ConversationRow, command.conversation_id)
        if conversation_row.character_id != command.character_id:
            raise InvalidStateError("character does not match the conversation")
        if conversation_row.state != ConversationState.ACTIVE.value:
            raise InvalidStateError("conversation is not active")
        if conversation_row.current_session_id is None:
            # 显式创建的空会话在首条消息的 T1 事务中建立初始 session。
            session_id = IdGenerator.new("session")
            db_session.add(conversation_mappers.session_to_row(Session.open(
                session_id, command.conversation_id, now)))
            await db_session.flush()
            conversation_row.current_session_id = session_id
            await db_session.flush()

        current_session = await db_session.get(SessionRow, conversation_row.current_session_id)
        if current_session.session_state == SessionState.WAITING_HANDOFF.value:
            raise InvalidStateError("当前窗口等待交接，请在交接完成后重试发送")
        if current_session.session_state != SessionState.OPEN.value:
            raise InvalidStateError("当前session不能接收新消息")

        # 部分唯一索引uq_turns_one_current保证一个session最多一个current turn
        current_turn_row = await db_session.scalar(
            select(TurnRow).where(
                TurnRow.session_id == conversation_row.current_session_id,
                TurnRow.turn_state == TurnState.CURRENT.value,
            )
        )

        if current_turn_row is None:
            # 上一轮已complete，开启下一turn，输入版本从1重新计数
            turn_id = IdGenerator.new("turn")
            last_turn_seq = await db_session.scalar(
                select(func.max(TurnRow.turn_seq)).where(
                    TurnRow.session_id == conversation_row.current_session_id)
            ) or 0
            db_session.add(conversation_mappers.turn_to_row(Turn.create(
                turn_id, conversation_row.current_session_id,
                last_turn_seq + 1, 1, TurnState.CURRENT)))
            await db_session.flush()
            input_version = 1
        else:
            turn_id = current_turn_row.turn_id
            # 追加输入使本轮input_version+1；row_version随行变更推进；
            # active_token+1让旧attempt永久失去采用资格，迟到的模型结果在T3被token拒绝
            current_turn_row.input_version += 1
            current_turn_row.row_version += 1
            current_turn_row.active_token += 1
            input_version = current_turn_row.input_version

        # message_seq由事务按turn内顺序分配，不依赖时间戳排序
        last_message_seq = await db_session.scalar(
            select(func.max(MessageRow.message_seq)).where(MessageRow.turn_id == turn_id)
        ) or 0
        message = Message.create_user(message_id, turn_id, last_message_seq + 1,
                                      command.request_id, command.content, now)
        db_session.add(conversation_mappers.message_to_row(message, client_created_at=command.client_created_at))
        await db_session.flush()

        if current_turn_row is not None:
            # 作废旧生成：attempt→superseded、job→obsolete，
            # 维持"非终态任务的expected_input_version等于turn当前版本"
            stale_attempts = (await db_session.scalars(
                select(GenerationAttemptRow).where(
                    GenerationAttemptRow.turn_id == turn_id,
                    GenerationAttemptRow.attempt_state.in_(
                        (AttemptState.RETRIEVING.value, AttemptState.GENERATING.value)),
                )
            )).all()
            for attempt_row in stale_attempts:
                attempt_row.attempt_state = AttemptState.SUPERSEDED.value
                attempt_row.updated_at = now
            stale_jobs = (await db_session.scalars(
                select(GenerationJobRow).where(
                    GenerationJobRow.turn_id == turn_id,
                    GenerationJobRow.state.in_((
                        GenerationJobStatus.NEED_PREPARE.value,
                        GenerationJobStatus.NEED_GENERATE.value,
                        GenerationJobStatus.NEED_ACCEPT.value,
                    )),
                )
            )).all()
            for job_row in stale_jobs:
                job_row.state = GenerationJobStatus.OBSOLETE.value
                job_row.updated_at = now
            await db_session.flush()

        # 登记针对新输入版本的生成任务
        job = GenerationJob.register_generate_reply(job_id, turn_id, input_version, now)
        db_session.add(reply_mappers.generation_job_to_row(job))
        await db_session.flush()

        result = SaveMessageResult(command.request_id, message_id, turn_id, input_version, job_id)
        receipt = RequestReceiptResult("send_message", command.request_id, payload_hash, result)
        db_session.add(conversation_mappers.receipt_to_row(receipt, created_at=now))
        await db_session.flush()
        return result

    @staticmethod
    def _message_payload_hash(command: SaveMessageInput) -> str:
        """Hash request semantics only; retries must preserve the original client time.

        Normalize equivalent timezone offsets, but retain exact content and identifiers.
        Server-generated IDs/timestamps and request_id are not part of the payload.

        输入:
        SaveMessageInput: {request_id, conversation_id, character_id, content, client_created_at}
        输出:
        str
        """
        timestamp = command.client_created_at
        payload = {
            "conversation_id": command.conversation_id,
            "character_id": command.character_id,
            "content": command.content,
            "client_created_at": timestamp.astimezone(timezone.utc).isoformat() if timestamp else None,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
