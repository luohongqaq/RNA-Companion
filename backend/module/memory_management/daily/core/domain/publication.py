"""Daily 审核后的待发布正文与索引表示准备。

DailyWrite 固定正文及其内容哈希，按非空记忆栏目构造对应的索引文本和分区表示。
该产物用于任务检查点与后续索引写入；正式版本的数据库激活由仓储完成。
"""
from __future__ import annotations
import hashlib
from module.shared.memory.types.indexing import RepresentationKind
from module.shared.memory.types.references import MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier
from dataclasses import dataclass, replace
from module.memory_management.daily.core.domain.document import DailyDocument
from module.memory_management.shared.indexing.core.domain.representation import IndexRepresentation


@dataclass(frozen=True, slots=True)
class DailyWrite:
    """结构化待发布正文检查点；正式激活前保存在任务中。"""
    document: DailyDocument
    content_hash: str

    @classmethod
    def from_document(cls, document: DailyDocument) -> DailyWrite:
        return cls(document, document.content_hash())

    def index_representations(self, ref: MemoryRevisionRef, character_id: str,
                              recall_tier: RecallTier) -> tuple[IndexRepresentation, ...]:
        """五个非空分区各一条表示，仅渲染正文，不放证据和审核批注。"""
        fields = ("memorable_moments", "profile_facts", "intentions", "commitments", "reflections")
        result = []
        for field in fields:
            if not getattr(self.document, field):
                continue
            section = replace(self.document, **{key: getattr(self.document, key) if key == field else () for key in fields})
            text = section.render_text()
            result.append(IndexRepresentation(ref, character_id, RepresentationKind.DAILY_MEMORY,
                field, text, hashlib.sha256(text.encode("utf-8")).hexdigest(), recall_tier))
        return tuple(result)
