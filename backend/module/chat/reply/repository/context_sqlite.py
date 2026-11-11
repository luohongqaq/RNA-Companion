"""固定上下文材料的SQLite读取器，每次调用使用独立短读事务。"""

from collections import defaultdict

from sqlalchemy import select

from module.shared.context.types.messages import CompletedTurnResult, MessageListItem
from module.shared.context.types.messages import MessageRole
from module.shared.common.errors.base import InvalidStateError, NotFoundError
from module.shared.context.types.references import SnapshotRef
from module.shared.context.types.working import SnapshotContext
from module.context_compaction.repository import mappers as context_mappers
from module.chat.conversation.repository import mappers as conversation_mappers
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import ContextSnapshotRow, MessageRow, TurnRow

# 控制IN参数个数，也支持使用较低SQLite变量上限的环境。
_BATCH_SIZE = 400

class SQLiteContextReadRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def read_snapshot(self, ref: SnapshotRef) -> SnapshotContext:
        """读取session唯一摘要：按snapshot_id取行、校验hash和覆盖点一致，
        再把summary_json还原成业务结构；只读取已采用的唯一摘要。

        输入:
        SnapshotRef: {snapshot_id, content_hash, covered_through_turn_seq}
        输出:
        SnapshotContext: {ref: SnapshotRef, summary_text, structured_summary: StructuredSummary}
        """

        async with self._database.db_session() as session:
            row = await session.get(ContextSnapshotRow, ref.snapshot_id)
            if row is None or context_mappers.snapshot_to_ref(row) != ref:
                raise NotFoundError(f"snapshot not found or reference mismatch: {ref.snapshot_id}")
            if row.snapshot_state != "valid":
                raise InvalidStateError("snapshot has not been adopted or is invalid")
            return context_mappers.snapshot_to_context(row)

    async def read_complete_turns(
        self, session_id: str, turn_seqs: tuple[int, ...],
    ) -> tuple[CompletedTurnResult, ...]:
        """加载会话中指定的若干完整轮：先查turn行并校验全部存在，再JOIN消息表一次取回这些轮的
        全部消息并按turn分组，最后逐轮组装"用户输入+正式采用的回复"；缺失或未完成的轮直接报错。

        输入:
        session_id, turn_seqs
        输出:
        CompletedTurnResult: {turn_id, session_id, turn_seq,
                              user_messages: tuple[MessageListItem, ...],
                              accepted_reply: MessageListItem}
        """
        if not turn_seqs:
            return ()
        # 重复序号输入直接拒绝
        if len(set(turn_seqs)) != len(turn_seqs):
            raise InvalidStateError(f"duplicate turn_seqs: {turn_seqs}")
        async with self._database.db_session() as session:
            # 一次性按session+seq查turn，by_seq = {turn_seq: TurnRow}
            turn_rows = (await session.scalars(select(TurnRow).where(
                TurnRow.session_id == session_id,
                TurnRow.turn_seq.in_(turn_seqs),
            ))).all()
            by_seq = {row.turn_seq: row for row in turn_rows}
            # 确认所有请求的turn都存在
            for seq in turn_seqs:
                if seq not in by_seq:
                    raise NotFoundError(f"complete turn not found: {session_id}@{seq}")
            # 经turns表JOIN按session+seq过滤消息，避免在Python里翻译turn_id
            messages = (await session.scalars(select(MessageRow).join(
                TurnRow, MessageRow.turn_id == TurnRow.turn_id,
            ).where(
                TurnRow.session_id == session_id,
                TurnRow.turn_seq.in_(turn_seqs),
            ).order_by(MessageRow.message_seq))).all()
            # 按turn_id分组message, grouped = {turn_id: [MessageRow, ...]}
            grouped = defaultdict(list)
            for message in messages:
                grouped[message.turn_id].append(message)
            return tuple(conversation_mappers.completed_turn_to_result(by_seq[seq], grouped[by_seq[seq].turn_id])
                         for seq in turn_seqs)

    async def read_user_messages(self, message_ids: tuple[str, ...]) -> tuple[MessageListItem, ...]:
        """按消息id批量读取用户消息：分批IN查询以适配SQLite变量上限，结果按传入顺序排列；
        任何一条缺失或角色不是user都直接报错。

        输入:
        message_ids
        输出:
        MessageListItem: {message_id, turn_id, message_seq, role, content,
                          visual: VisualState|None, created_at}
        """
        if not message_ids:
            return ()
        requested = tuple(dict.fromkeys(message_ids))
        async with self._database.db_session() as session:
            rows = []
            for offset in range(0, len(requested), _BATCH_SIZE):
                rows.extend((await session.scalars(select(MessageRow).where(
                    MessageRow.message_id.in_(requested[offset:offset + _BATCH_SIZE]),
                ))).all())
            by_id = {row.message_id: row for row in rows}
            result = []
            for message_id in message_ids:
                row = by_id.get(message_id)
                if row is None:
                    raise NotFoundError(f"user message not found: {message_id}")
                if row.role != MessageRole.USER.value:
                    raise InvalidStateError(f"message is not a user input: {message_id}")
                result.append(conversation_mappers.message_to_list_item(row))
            return tuple(result)
