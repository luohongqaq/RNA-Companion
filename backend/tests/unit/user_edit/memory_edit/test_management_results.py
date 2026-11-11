"""用户查询投影保留可编辑内容，同时隔离内部证据。"""
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
import json

import pytest

from module.shared.common.types.pagination import Page
from module.user_edit.memory_edit.core.domain.document import DailyDocumentContent, TopicDocumentContent
from module.user_edit.memory_edit.core.domain.result import MemoryDetails
from module.memory_management.daily.core.domain.document import DailyDocument
from module.memory_management.topic.core.domain.document import TopicCurrentState, TopicDocument, TopicEntry, TopicIdentity
from module.memory_management.shared.core.domain.document import TopicMemory
from module.shared.memory.types.references import EvidenceRef, MemoryKind


def test_daily_projection_preserves_all_sections_without_evidence():
    data = dict(
        memory_date="2026-11-04", source_session_ids=["private-session"], title="用户标题",
        memorable_moments=[dict(kind="milestone", text="重要时刻", source_turn_ids=["private-turn"])],
        profile_facts=[dict(kind="preference", text="偏好", source_turn_ids=["private-turn"], evidence_quotes=["private-quote"])],
        intentions=[dict(owner="user", certainty="plan", status="open", text="计划", target_date="2026-11-09",
                        source_turn_ids=[], evidence_quotes=[])],
        commitments=[dict(owner="character", status="open", text="承诺", target_date=None, source_turn_ids=[])],
        reflections=[dict(text="反思", source_turn_ids=[])],
    )
    source = DailyDocument.from_data(data)
    public = DailyDocumentContent.from_document(source)
    assert public.memory_date == date(2026, 11, 4) and public.title == "用户标题"
    assert [public.memorable_moments[0].text, public.profile_facts[0].text,
            public.intentions[0].text, public.commitments[0].text, public.reflections[0].text] == ["重要时刻", "偏好", "计划", "承诺", "反思"]
    assert public.intentions[0].target_date == date(2026, 11, 9)
    assert public.commitments[0].target_date is None
    serialized = json.dumps(asdict(public), default=str)
    for private in ("source_turn_ids", "source_session_ids", "evidence_quotes", "private-"):
        assert private not in serialized
    assert source.to_data() == data  # 投影不能修改正式来源。


def test_topic_details_keep_ids_corrections_and_version_without_storage_fields():
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    evidence = EvidenceRef("private-session", "private-message", "private-quote")
    document = TopicDocument("项目", TopicIdentity("主题说明", (evidence,)),
        TopicCurrentState("当前进度", ("corrected",)), (
            TopicEntry("old", "原记录", None, now.date(), (evidence,)),
            TopicEntry("corrected", "更正记录", now.date(), now.date(), (evidence,), corrects_entry_id="old"),
            TopicEntry("plan", "未来计划", None, now.date(), (), kind="plan", target_time=date(2026, 11, 9)),
        ))
    memory = TopicMemory(document_id="topic-1", kind=MemoryKind.TOPIC, current_revision=3,
        content_hash="private-hash", vector_document_key="private-vector", published_at=now,
        updated_at=now, source_refs=(evidence,), document=document)
    details = MemoryDetails.from_memory(memory, character_id="character-1")
    assert (details.document_id, details.character_id, details.revision, details.updated_at) == ("topic-1", "character-1", 3, now)
    assert details.document.entries[1].corrects_entry_id == "old"
    assert details.document.entries[2].target_time == date(2026, 11, 9)
    assert details.document.current_state.source_entry_ids == ("corrected",)
    serialized = json.dumps(asdict(details), default=str)
    for private in ("source_refs", "quote", "content_hash", "vector_document_key", "published_at", "private-"):
        assert private not in serialized
    with pytest.raises(ValueError, match="类型一致"):
        MemoryDetails.from_memory(replace(memory, kind=MemoryKind.DAILY), character_id="character-1")


def test_empty_documents_remain_displayable():
    document = TopicDocument("空主题", TopicIdentity("说明", ()), TopicCurrentState("", ()), ())
    public = TopicDocumentContent.from_document(document)
    assert public.entries == () and public.current_state.source_entry_ids == ()
    assert Page((), None).next_cursor is None
