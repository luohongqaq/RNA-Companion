"""Topic 审核后的正文与固定版本发布产物。

TopicWrite 保存正文哈希并构造分区索引表示，TopicWriteItem 将审核通过的申请转换为带版本引用的待发布项。
供批量索引、任务恢复和正式激活复用同一内容与版本，避免重试时重新分配发布身份。
"""
from __future__ import annotations
import hashlib
from module.shared.memory.types.indexing import RepresentationKind
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier
from dataclasses import dataclass
from module.memory_management.shared.indexing.core.domain.representation import IndexRepresentation
from module.memory_management.topic.core.domain.document import TopicDocument
from module.memory_management.topic.core.domain.generation import TopicApplication


@dataclass(frozen=True, slots=True)
class TopicWrite:
    """一篇已审核topic的待发布正文；批量job可保存多份，不代表已正式激活。"""
    document: TopicDocument
    content_hash: str

    @classmethod
    def from_document(cls, document: TopicDocument) -> TopicWrite:
        return cls(document, document.content_hash())

    def index_representations(self, ref: MemoryRevisionRef, character_id: str,
                              recall_tier: RecallTier) -> tuple[IndexRepresentation, ...]:
        """三个非空分区各一条表示；名称贯穿各分区，不额外编码日期和来源信息。"""
        if ref.kind != MemoryKind.TOPIC or ref.content_hash != self.content_hash:
            raise ValueError("topic索引引用与待发布正文不一致")
        sections = (
            (RepresentationKind.TOPIC_NAME, self.document.topic),
            (RepresentationKind.TOPIC_IDENTITY, self.document.topic_identity.description),
            (RepresentationKind.TOPIC_CURRENT, self.document.current_state.text),
        )
        result = []
        for kind, body in sections:
            if not body.strip():
                continue
            text = body if kind == RepresentationKind.TOPIC_NAME else f"{self.document.topic}\n{body}"
            result.append(IndexRepresentation(ref, character_id, kind, kind.value, text,
                hashlib.sha256(text.encode("utf-8")).hexdigest(), recall_tier))
        return tuple(result)


@dataclass(frozen=True, slots=True)
class TopicWriteItem:
    """批量待发布项；将结构化正文绑定申请和稳定的目标版本。"""
    application_id: str
    ref: MemoryRevisionRef
    write: TopicWrite

    def __post_init__(self) -> None:
        if (not self.application_id or self.ref.kind != MemoryKind.TOPIC
                or self.ref.content_hash != self.write.content_hash):
            raise ValueError("topic待发布项的申请、版本与正文不一致")

    @classmethod
    def from_application(cls, application: TopicApplication) -> TopicWriteItem:
        write = TopicWrite.from_document(application.document)
        base = application.base_ref
        ref = MemoryRevisionRef(MemoryKind.TOPIC, application.topic_id,
            base.revision + 1 if base is not None else 1, write.content_hash)
        return cls(application.application_id, ref, write)
