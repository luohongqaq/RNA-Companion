"""可供召回使用的正式记忆文档。

RecallDocument 将 Daily 或 Topic 正式正文与角色、冷热池和固定版本引用绑定。
正文仓储负责读取并核验当前生效版本，检索业务以此为准，不使用向量 payload 替代正式正文。
"""
from __future__ import annotations
from module.shared.memory.types.references import MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier
from dataclasses import dataclass
from module.memory_management.daily.core.domain.document import DailyDocument
from module.memory_management.topic.core.domain.document import TopicDocument


@dataclass(frozen=True, slots=True)
class RecallDocument:
    ref: MemoryRevisionRef
    character_id: str
    recall_tier: RecallTier
    document: DailyDocument | TopicDocument

    def to_recall_payload(self) -> dict:
        document = self.document
        public = document.public_payload()
        if isinstance(document, DailyDocument):
            title = document.title
            sections = {key: public[key] for key in (
                "memorable_moments", "profile_facts", "intentions", "commitments", "reflections") if public[key].strip()}
            dates = [document.memory_date.isoformat()]
            events = []
        else:
            title = document.topic
            sections = {key: public[key] for key in ("topic_identity", "current_state", "entries") if public[key].strip()}
            dates = sorted({event.acquire_time.isoformat() for event in document.entries})
            # 更正后的旧事件保留在公开时间线中，但不作为当前事件时间过滤的命中依据。
            superseded = {event.corrects_entry_id for event in document.entries if event.corrects_entry_id}
            events = [dict(entry_id=event.entry_id, kind=event.kind,
                occurrence_time=event.occurrence_time.isoformat() if event.occurrence_time else None,
                acquire_time=event.acquire_time.isoformat(),
                target_time=event.target_time.isoformat() if event.target_time else None,
                superseded=event.entry_id in superseded) for event in document.entries]
        return dict(document_id=self.ref.memory_id, kind=self.ref.kind.value, character_id=self.character_id,
            revision=self.ref.revision, content_hash=self.ref.content_hash, pool=self.recall_tier.value,
            title=title, sections=sections, conversation_time=None, record_dates=dates,
            memory_date=document.memory_date.isoformat() if isinstance(document, DailyDocument) else None,
            temporal_events=events, _ref=self.ref,
            match_text="\n".join((title, *(value for key, value in sections.items() if key != "entries"))))
