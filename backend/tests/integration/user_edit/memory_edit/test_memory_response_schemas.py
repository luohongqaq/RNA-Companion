"""记忆查询结果到 HTTP JSON 的响应边界。"""
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from api.schemas.memory import (
    MemoryDetailsResponse, MemoryListItemResponse, MemoryEditResponse,
    UpdateDailyMemoryRequest, UpdateTopicMemoryRequest,
)
from module.user_edit.memory_edit.core.domain.result import MemoryEditResult
from module.user_edit.memory_edit.core.domain.job import MemoryEditStatus
from module.shared.common.types.pagination import Page
from module.user_edit.memory_edit.core.domain.document import DailyDocumentContent, DailyIntentionContent, TopicDocumentContent, TopicEntryContent, TopicIdentityContent
from module.user_edit.memory_edit.core.domain.result import MemoryDetails, MemoryListItem
from module.memory_management.daily.core.domain.document import IntentionCertainty, IntentionOwner, IntentionStatus
from module.memory_management.topic.core.domain.document import TopicCurrentState, TopicDocument, TopicEntry, TopicIdentity
from module.shared.memory.types.references import EvidenceRef, MemoryKind

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
DAY = NOW.date()


def details(kind):
    if kind == MemoryKind.DAILY:
        document = DailyDocumentContent(DAY, "日记", intentions=(DailyIntentionContent(
            IntentionOwner.USER, IntentionCertainty.PLAN, IntentionStatus.OPEN, "继续项目", None),))
    else:
        document = TopicDocumentContent("项目", TopicIdentityContent("开发项目"),
            TopicCurrentState("已更正", ("new",)), (
                TopicEntryContent("old", "原记录", "event", "user", DAY, None, None, None),
                TopicEntryContent("new", "更正记录", "event", "user", DAY, DAY, None, "old"),
                TopicEntryContent("plan", "未来计划", "plan", "assistant", DAY, None, date(2026, 11, 9), None),
            ))
    return MemoryDetails("memory-1", "character-1", kind, 3, NOW, document)


@pytest.mark.parametrize("kind", [MemoryKind.DAILY, MemoryKind.TOPIC])
def test_details_convert_business_attributes_and_serialize_json(kind):
    response = MemoryDetailsResponse.model_validate(details(kind))
    data = response.model_dump(mode="json")
    assert data["kind"] == kind.value and data["revision"] == 3
    assert data["updated_at"] == "2026-11-04T12:00:00Z"
    if kind == MemoryKind.DAILY:
        assert data["document"]["memory_date"] == "2026-11-04"
        assert data["document"]["intentions"][0]["owner"] == "user"
        assert data["document"]["intentions"][0]["target_date"] is None
        assert data["document"]["reflections"] == []
    else:
        assert data["document"]["entries"][1]["corrects_entry_id"] == "old"
        assert data["document"]["entries"][2]["target_time"] == "2026-11-09"
        assert data["document"]["current_state"]["source_entry_ids"] == ["new"]


def test_page_converts_tuple_and_retains_cursor():
    result = Page[MemoryListItem]((MemoryListItem("id", MemoryKind.TOPIC, "标题", 1, NOW),), "cursor-1")
    response = Page(items=tuple(MemoryListItemResponse.model_validate(item) for item in result.items), next_cursor=result.next_cursor)
    adapter = TypeAdapter(Page[MemoryListItemResponse])
    data = adapter.dump_python(response, mode="json")
    assert isinstance(data["items"], list) and data["items"][0]["title"] == "标题"
    assert data["next_cursor"] == "cursor-1"
    assert adapter.dump_python(Page((), None), mode="json") == {"items": [], "next_cursor": None}


def test_response_does_not_serialize_internal_evidence():
    evidence = EvidenceRef("private-session", "private-message", "private-quote")
    source = TopicDocument("项目", TopicIdentity("说明", (evidence,)), TopicCurrentState("", ()),
        (TopicEntry("old", "原记录", None, DAY, (evidence,)),))
    result = replace(details(MemoryKind.TOPIC), document=TopicDocumentContent.from_document(source))
    response = MemoryDetailsResponse.model_validate(result)
    serialized = response.model_dump_json()
    assert "private-" not in serialized and "source_refs" not in serialized


@pytest.mark.parametrize("kind", [MemoryKind.DAILY, MemoryKind.TOPIC])
def test_kind_cannot_be_mismatched_with_document(kind):
    other = MemoryKind.TOPIC if kind == MemoryKind.DAILY else MemoryKind.DAILY
    with pytest.raises(ValidationError, match="正文类型不一致"):
        MemoryDetailsResponse.model_validate(replace(details(kind), kind=other))


def test_invalid_revision_and_unsupported_kind_are_rejected():
    with pytest.raises(ValidationError):
        MemoryDetailsResponse.model_validate(replace(details(MemoryKind.DAILY), revision=0))
    with pytest.raises(ValidationError):
        MemoryDetailsResponse.model_validate(replace(details(MemoryKind.DAILY), kind=MemoryKind.OBJECT))


@pytest.mark.parametrize("kind,request_type", [
    (MemoryKind.DAILY, UpdateDailyMemoryRequest), (MemoryKind.TOPIC, UpdateTopicMemoryRequest),
])
def test_edit_request_reuses_public_document_and_preserves_content(kind, request_type):
    body = MemoryDetailsResponse.model_validate(details(kind)).model_dump(mode="json")["document"]
    field = "title" if kind == MemoryKind.DAILY else "topic"
    body[field] = "  用户标题\n"
    request = request_type.model_validate(dict(character_id="c", expected_revision=3,
        idempotency_key="request-1", document=body))
    assert request.document == replace(details(kind).document, **{field: "  用户标题\n"})
    assert request.model_dump(mode="json")["document"] == body
    with pytest.raises(ValidationError):
        request_type.model_validate(request.model_dump() | {"document_id": "path-only"})
    with pytest.raises(ValidationError):
        request_type.model_validate(request.model_dump() | {"document": body | {"source_refs": []}})


@pytest.mark.parametrize("changes", [dict(expected_revision=0), dict(expected_revision=True),
    dict(expected_revision="3"), dict(character_id=" "), dict(idempotency_key=""), dict(idempotency_key=" " * 3)])
def test_edit_request_rejects_invalid_metadata(changes):
    with pytest.raises(ValidationError):
        UpdateDailyMemoryRequest.model_validate(dict(character_id="c", expected_revision=3,
            idempotency_key="request-1", document=details(MemoryKind.DAILY).document) | changes)


@pytest.mark.parametrize("status", list(MemoryEditStatus))
def test_edit_result_converts_business_attributes(status):
    result = MemoryEditResult("edit-1", "memory-1", MemoryKind.TOPIC, 4, status)
    data = MemoryEditResponse.model_validate(result).model_dump(mode="json")
    assert data == dict(edit_id="edit-1", document_id="memory-1", kind="topic", revision=4,
                        index_status=status.value)
