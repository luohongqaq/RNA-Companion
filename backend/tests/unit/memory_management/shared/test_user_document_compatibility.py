"""用户无来源内容的存储协议兼容，以及 AI 入口校验边界。"""
from copy import deepcopy
from datetime import date

import pytest

from module.shared.common.errors.base import InvalidStateError
from module.memory_management.daily.core.domain.document import DailyDocument
from module.memory_management.daily.core.domain.publication import DailyWrite
from module.memory_management.topic.core.domain.document import TopicCurrentState, TopicDocument, TopicEntry, TopicIdentity
from module.memory_management.topic.core.domain.publication import TopicWrite
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier
from module.memory_management.shared.repository import document_mappers


def daily_data():
    return {
        "memory_date": "2026-11-04", "source_session_ids": [], "title": "用户修改的标题",
        "memorable_moments": [{"kind": "milestone", "text": "手动时刻", "source_turn_ids": []}],
        "profile_facts": [{"kind": "preference", "text": "手动事实", "source_turn_ids": [], "evidence_quotes": []}],
        "intentions": [{"owner": "user", "certainty": "plan", "status": "open", "text": "手动打算",
                        "target_date": None, "source_turn_ids": [], "evidence_quotes": []}],
        "commitments": [{"owner": "character", "status": "open", "text": "手动承诺",
                         "target_date": None, "source_turn_ids": []}],
        "reflections": [{"text": "手动反思", "source_turn_ids": []}],
    }


def test_user_daily_round_trip_render_and_index_without_evidence():
    document = DailyDocument.from_data(daily_data())
    restored = document_mappers._memory_daily_document_from_json(document_mappers._memory_daily_document_to_json(document))
    assert restored == document and restored.to_data() == daily_data()
    write = DailyWrite.from_document(restored)
    assert write.document.render_text().startswith("# 用户修改的标题\n")
    for text in ("手动时刻", "手动事实", "手动打算", "手动承诺", "手动反思"):
        assert text in write.document.render_text()
    ref = MemoryRevisionRef(MemoryKind.DAILY, "daily_user", 2, write.content_hash)
    sections = write.index_representations(ref, "c", RecallTier.HOT)
    assert len(sections) == 5 and all(section.ref == ref for section in sections)


@pytest.mark.parametrize("field", ["profile_facts", "intentions"])
@pytest.mark.parametrize("quotes", [None, "原话", [" "], [1]])
def test_user_daily_still_rejects_invalid_evidence_type(field, quotes):
    data = daily_data()
    data[field][0]["evidence_quotes"] = quotes
    with pytest.raises(InvalidStateError):
        DailyDocument.from_data(data)


@pytest.mark.parametrize("field", ["profile_facts", "intentions"])
@pytest.mark.parametrize("missing", ["source_turn_ids", "evidence_quotes"])
def test_ai_daily_requires_sources_and_quotes(field, missing):
    data = daily_data()
    data.pop("memory_date")
    data.pop("source_session_ids")
    data["session_id"] = "session"
    for items in (data[key] for key in ("memorable_moments", "profile_facts", "intentions", "commitments", "reflections")):
        for item in items:
            item["source_turn_ids"] = ["turn"]
            if "evidence_quotes" in item:
                item["evidence_quotes"] = ["原话"]
    valid = DailyDocument.from_model_data(deepcopy(data), session_id="session",
        memory_date=date(2026, 11, 4), source_session_ids=("session",), allowed_turn_ids={"turn"})
    assert valid.collect_source_turn_ids() == {"turn"}
    data[field][0][missing] = []
    with pytest.raises(InvalidStateError):
        DailyDocument.from_model_data(data, session_id="session", memory_date=date(2026, 11, 4),
                                    source_session_ids=("session",), allowed_turn_ids={"turn"})


def test_user_topic_round_trip_render_and_index_without_sources():
    event = TopicEntry("manual_entry", "用户记录的进度", None, date(2026, 11, 4), ())
    document = TopicDocument("用户主题", TopicIdentity("用户主题说明", ()),
                             TopicCurrentState("用户修改的当前状态", ()), (event,))
    restored = document_mappers._memory_topic_document_from_json(document_mappers._memory_topic_document_to_json(document))
    assert restored == document and restored.entries[0].source_refs == ()
    write = TopicWrite.from_document(restored)
    assert "用户记录的进度" in write.document.render_text()
    ref = MemoryRevisionRef(MemoryKind.TOPIC, "topic_user", 2, write.content_hash)
    assert len(write.index_representations(ref, "c", RecallTier.HOT)) == 3


@pytest.mark.parametrize("changes", [
    {"entry_id": ""}, {"statement": " "}, {"kind": "invalid"},
    {"acquire_time": "2026-11-04"}, {"kind": "plan", "occurrence_time": date(2026, 11, 4)},
])
def test_user_topic_still_enforces_structure(changes):
    fields = dict(entry_id="entry", statement="进度", occurrence_time=None,
                  acquire_time=date(2026, 11, 4), source_refs=())
    with pytest.raises(ValueError):
        TopicEntry(**(fields | changes))
