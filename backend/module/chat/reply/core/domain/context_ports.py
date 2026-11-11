"""回复准备按固定引用读取正文的接口。"""
from __future__ import annotations
from typing import Protocol
from module.shared.context.types.messages import CompletedTurnResult, MessageListItem
from module.shared.context.types.references import SnapshotRef
from module.shared.context.types.working import SnapshotContext


class ContextReadRepository(Protocol):
    async def read_snapshot(
        self,
        ref: SnapshotRef,
    ) -> SnapshotContext:
        """按固定ID读取session唯一已采用的摘要；缺失时抛NotFoundError。

        输入:
        SnapshotRef: {snapshot_id, content_hash, covered_through_turn_seq}
        输出:
        SnapshotContext: {ref: SnapshotRef, summary_text, structured_summary: StructuredSummary}
        """
        ...

    async def read_complete_turns(
        self,
        session_id: str,
        turn_seqs: tuple[int, ...],
    ) -> tuple[CompletedTurnResult, ...]:
        """完整返回指定Session中的轮，按请求顺序排列；序号重复抛InvalidStateError，缺失或未完成明确报错。

        输入:
        session_id, turn_seqs
        输出:
        tuple[CompletedTurnResult, ...]，元素:
        CompletedTurnResult: {turn_id, session_id, turn_seq,
                              user_messages: tuple[MessageListItem, ...],
                              accepted_reply: MessageListItem}
        """
        ...

    async def read_user_messages(
        self,
        message_ids: tuple[str, ...],
    ) -> tuple[MessageListItem, ...]:
        """完整返回指定ID对应的用户消息，保持请求顺序及重复项，不扩展当前输入。

        任一消息缺失时抛NotFoundError；任一消息不是user时抛InvalidStateError。
        空请求返回空元组，不得静默跳过缺失项或返回部分结果。

        输入:
        message_ids
        输出:
        tuple[MessageListItem, ...]，元素:
        MessageListItem: {message_id, turn_id, message_seq, role, content,
                          visual: VisualState|None, created_at}
        """
        ...
