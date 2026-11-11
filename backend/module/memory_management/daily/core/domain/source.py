"""Daily 生成和审核使用的已加载来源材料。

DailySourceInput 组合固定计划、来源清单、归属轮次、辅助上文和当天已有正文，并生成模型输入。
连续性说明和继承摘要只辅助理解；本文件明确新增事实可以引用的轮次范围。
"""
from __future__ import annotations
from module.shared.character.document import CharacterSelectionView
from module.shared.common.time import BackendTime
from module.shared.context.types.messages import CompletedTurnResult
from module.shared.context.types.working import InheritedContext
from dataclasses import asdict, dataclass
from module.memory_management.daily.core.domain.document import DailyDocument
from module.memory_management.daily.core.domain.plan import DailyPlan
from module.memory_management.shared.contracts.source import MemorySourceManifest


@dataclass(frozen=True, slots=True)
class SessionContinuation:
    session_id: str
    continuation: str | None
    continuation_version: int | None


@dataclass(frozen=True, slots=True)
class ContinuationEpisodeSummary:
    """仅帮助消歧与连续性，不是本次Daily的事实证据。"""
    session_id: str
    revision: int
    content_hash: str
    episode_summary: str


@dataclass(frozen=True, slots=True)
class DailySourceInput:
    """按固定计划加载的正文；辅助上文不扩展本次新增事实范围。"""
    plan: DailyPlan
    source_manifest: MemorySourceManifest
    owned_turns: tuple[CompletedTurnResult, ...]
    context_only_turns: tuple[CompletedTurnResult, ...] = ()
    base_daily: DailyDocument | None = None
    inherited_context: InheritedContext | None = None

    def allowed_source_turn_ids(self) -> set[str]:
        """本次事实引用来自拥有的完整轮；已有daily的记录允许沿用原引用。"""
        allowed = {turn.turn_id for turn in self.owned_turns}
        if self.base_daily is not None:
            allowed.update(self.base_daily.collect_source_turn_ids())
        return allowed

    def to_model_payload(self, *, character_context: CharacterSelectionView | None = None) -> dict:
        snapshot = self.inherited_context.snapshot if self.inherited_context is not None else None
        payload = {
            "SESSION_META": {"session_id": self.plan.session_id, "character_id": self.plan.character_id,
                             "memory_date": self.plan.memory_date.isoformat(), "timezone": "backend_local"},
            "SESSION_TURNS": [self._turn_payload(turn) for turn in self.owned_turns],
            "BASE_DAILY": self.base_daily.to_data() if self.base_daily is not None else None,
            "CONTEXT_ONLY_TURNS": [self._turn_payload(turn) for turn in self.context_only_turns],
            "CONTINUATION_EPISODE_SUMMARY": snapshot.summary_text if snapshot is not None else None,
            "INHERITED_CONTEXT": asdict(self.inherited_context) if self.inherited_context is not None else None,
            "ALLOWED_SOURCE_TURN_IDS": sorted(self.allowed_source_turn_ids()),
        }
        if character_context is not None:
            payload["CHARACTER_CONTEXT"] = character_context.selection_context()
        return payload

    @staticmethod
    def _turn_payload(turn: CompletedTurnResult) -> dict:
        payload = asdict(turn)
        for message in (*payload["user_messages"], payload["accepted_reply"]):
            timestamp = message["created_at"]
            local = BackendTime.local(timestamp)
            message["created_at"] = BackendTime.utc(timestamp)
            message["local_date"] = local.date().isoformat()
            message["local_utc_offset"] = local.strftime("%z")
        return payload
