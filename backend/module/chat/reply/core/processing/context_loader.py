"""每次按固定引用从存储加载正文；返回对象由调用方直接复用。"""

from module.shared.context.types.messages import CompletedTurnResult
from module.shared.context.types.turns import CurrentTurnRef, TurnRange
from module.shared.common.errors.base import InvalidStateError, NotFoundError
from module.chat.reply.core.domain.context_ports import ContextReadRepository
from module.shared.context.types.references import InheritedContextRef, SnapshotRef
from module.shared.context.types.working import CurrentTurnInput, InheritedContext, SnapshotContext, WorkingContext

class ContextLoader:
    """加载器只持有读取器，不保留材料缓存，也不检查生成资格。"""

    def __init__(self, repository: ContextReadRepository) -> None:
        self._repository = repository

    async def load(
        self,
        *,
        working_snapshot_ref: SnapshotRef | None,
        complete_turns: TurnRange,
        current_turn_ref: CurrentTurnRef,
        inherited_context_ref: InheritedContextRef | None,
    ) -> WorkingContext:
        """调用ports的接口从数据库中读取对应引用的数据

        输入:
        SnapshotRef: {snapshot_id, content_hash, covered_through_turn_seq}|None
        TurnRange: {session_id, from_seq, through_seq}
        CurrentTurnRef: {turn_id, input_version, message_ids}
        InheritedContextRef: {predecessor_session_id, summary_snapshot_ref: SnapshotRef|None,
                              source_range: TurnRange|None, source_hash}|None
        输出:
        WorkingContext: {working_snapshot: SnapshotContext|None,
                         complete_turns: tuple[CompletedTurnResult, ...],
                         current_input: CurrentTurnInput, inherited_context: InheritedContext|None}
        """
        return WorkingContext(
            working_snapshot=await self._load_snapshot(working_snapshot_ref),
            complete_turns=await self._load_range(complete_turns),
            current_input=await self._load_current_input(current_turn_ref),
            inherited_context=await self._load_inherited(inherited_context_ref),
        )

    async def _load_snapshot(self, ref: SnapshotRef | None) -> SnapshotContext | None:
        """按需加载快照正文：给了引用就读，没给引用返回None。

        输入:
        SnapshotRef: {snapshot_id, content_hash, covered_through_turn_seq}|None
        输出:
        SnapshotContext: {ref: SnapshotRef, summary_text, structured_summary: StructuredSummary}|None
        """
        return await self._repository.read_snapshot(ref) if ref is not None else None

    async def _load_range(self, turn_range: TurnRange | None) -> tuple[CompletedTurnResult, ...]:
        """把连续轮次范围展开成逐个序号后加载；范围为空或起止倒挂时返回空元组。

        输入:
        TurnRange: {session_id, from_seq, through_seq}|None
        输出:
        tuple[CompletedTurnResult, ...]，元素:
        CompletedTurnResult: {turn_id, session_id, turn_seq,
                              user_messages: tuple[MessageListItem, ...],
                              accepted_reply: MessageListItem}
        """
        if turn_range is None or turn_range.from_seq > turn_range.through_seq:
            return ()
        return await self._load_complete_turns(
            turn_range.session_id, tuple(range(turn_range.from_seq, turn_range.through_seq + 1)),
        )

    async def _load_complete_turns(self, session_id: str, turn_seqs: tuple[int, ...]) -> tuple[CompletedTurnResult, ...]:
        """直接委托给读取器：结果由读取器保证齐全且按请求顺序排列，本层不做缓存和兜底。

        输入:
        session_id, turn_seqs
        输出:
        tuple[CompletedTurnResult, ...]，元素:
        CompletedTurnResult: {turn_id, session_id, turn_seq,
                              user_messages: tuple[MessageListItem, ...],
                              accepted_reply: MessageListItem}
        """
        return await self._repository.read_complete_turns(session_id, turn_seqs)

    async def _load_current_input(self, ref: CurrentTurnRef) -> CurrentTurnInput:
        """读取当前轮引用指名的用户消息：按引用去重后读取，校验每条都存在且都属于本轮，
        再按引用的原始顺序组装。

        输入:
        CurrentTurnRef: {turn_id, input_version, message_ids}
        输出:
        CurrentTurnInput: {turn_ref: CurrentTurnRef, user_messages: tuple[MessageListItem, ...]}
        """
        loaded = await self._repository.read_user_messages(ref.message_ids) if ref.message_ids else ()
        if any(message.turn_id != ref.turn_id for message in loaded):
            raise InvalidStateError(f"user message belongs to another turn: {ref.turn_id}")
        return CurrentTurnInput(ref, loaded)

    async def _load_inherited(self, ref: InheritedContextRef | None) -> InheritedContext | None:
        """加载前驱会话的继承上下文：前驱摘要正文（若有）加上摘要覆盖点之后的尾部完整轮原文。

        输入:
        InheritedContextRef: {predecessor_session_id, summary_snapshot_ref: SnapshotRef|None,
                              source_range: TurnRange|None, source_hash}|None
        输出:
        InheritedContext: {ref: InheritedContextRef, snapshot: SnapshotContext|None,
                           complete_turns: tuple[CompletedTurnResult, ...], memory_processing_pending}
        """
        if ref is None:
            return None
        return InheritedContext(
            ref=ref, snapshot=await self._load_snapshot(ref.summary_snapshot_ref),
            complete_turns=await self._load_range(ref.source_range),
        )
