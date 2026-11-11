"""摘要生成的模型输入；来源边界由程序固定。"""
from dataclasses import asdict, dataclass

from module.shared.character.document import CharacterSelectionView
from module.shared.context.types.messages import CompletedTurnResult
from module.context_compaction.core.domain.plan import CompactionPlan
from module.shared.context.types.references import ContextSourceRef
from module.context_compaction.core.domain.snapshots import SummarySourceManifest
from module.shared.context.types.working import InheritedContext


@dataclass(frozen=True, slots=True)
class SummaryModelInput:
    plan: CompactionPlan
    source_manifest: SummarySourceManifest
    owned_turns: tuple[CompletedTurnResult, ...]
    context_only_turns: tuple[CompletedTurnResult, ...] = ()
    inherited_context: InheritedContext | None = None
    character_id: str | None = None #session所属角色，由仓储按conversation读取

    def collect_allowed_source_refs(self) -> set[ContextSourceRef]:
        """本次原文以及固定继承摘要中允许沿用的来源引用。"""
        allowed = set(self.source_manifest.source_refs)
        if self.inherited_context is not None and self.inherited_context.snapshot is not None:
            allowed.update(self.inherited_context.snapshot.structured_summary.collect_source_refs())
        return allowed

    def to_model_payload(self, *, character_context: CharacterSelectionView | None = None) -> dict:
        """明确区分拥有的原文、继承状态与仅供理解的边界参考。"""
        policy = self.plan.compaction_policy
        payload = {
            "owned_turns": [asdict(turn) for turn in self.owned_turns],
            "inherited_context": asdict(self.inherited_context) if self.inherited_context is not None else None,
            "context_only_turns": [asdict(turn) for turn in self.context_only_turns],
            "allowed_source_refs": [asdict(ref) for ref in sorted(self.collect_allowed_source_refs(),
                key=lambda ref: (ref.session_id or "", ref.turn_id, ref.revision))],
            "summary_target_tokens": policy.summary_target_tokens,
            "summary_token_limit": policy.summary_token_limit,
        }
        if character_context is not None:
            payload["CHARACTER_CONTEXT"] = character_context.selection_context()
        return payload
