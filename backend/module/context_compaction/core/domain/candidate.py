"""固定来源的生成候选与绑定版本、哈希的程序校验结果。"""
from __future__ import annotations
from module.context_compaction.core.domain.snapshots import SummarySourceManifest
from module.shared.context.types.summary import StructuredSummary, SummaryVersions
from dataclasses import dataclass
from datetime import datetime, timezone

from module.chat.reply.core.domain.ports import TokenCounter
from module.shared.common.errors.base import InvalidStateError
from module.shared.context.types.messages import PromptMessage


@dataclass(frozen=True, slots=True)
class SummaryCandidate:
    candidate_version: int #本代次候选标识，不表示发布多份摘要
    source_manifest: SummarySourceManifest
    covered_through_turn_seq: int
    summary: StructuredSummary
    summary_text: str
    token_count: int
    content_hash: str
    versions: SummaryVersions
    generated_at: datetime
    attempt_version: int #候选生成时的执行代次；不随校验执行代次变化


    @classmethod
    def from_summary(
        cls,
        *,
        summary: StructuredSummary,
        manifest: SummarySourceManifest,
        attempt_version: int,
        candidate_version: int,
        versions: SummaryVersions,
        token_counter: TokenCounter,
    ) -> SummaryCandidate:
        """根据压缩清单生成候选摘要"""
        if manifest.source_turns is None:
            raise InvalidStateError("候选缺少固定来源范围")
        if versions.tokenizer_version != token_counter.tokenizer_version:
            raise InvalidStateError("候选tokenizer版本与计数器不一致")
        text = summary.render()
        return cls(
            candidate_version=candidate_version, source_manifest=manifest,
            covered_through_turn_seq=manifest.source_turns.through_seq,
            summary=summary, summary_text=text,
            token_count=token_counter.count_messages((PromptMessage.reference("上一会话摘要", text),)),
            content_hash=summary.content_hash(), versions=versions,
            generated_at=datetime.now(timezone.utc), attempt_version=attempt_version,
        )

@dataclass(frozen=True, slots=True)
class SummaryValidation:
    candidate_version: int #必须对应被校验候选的版本
    passed: bool
    issues: tuple[str, ...]
    validation_version: str #程序校验规则版本
    candidate_hash: str #绑定实际校验内容，避免只校验版本号
