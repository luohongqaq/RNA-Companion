"""主题任务、计划、候选和审核的持久化映射。"""

from typing import Any
from module.shared.memory.types.references import MemoryKind
from module.memory_management.topic.core.domain.job import TopicJob, TopicJobInput
from module.memory_management.topic.core.domain.source import TopicSourceInput, TopicObjectGroup
from module.memory_management.topic.core.domain.source import TopicContext
from module.memory_management.shared.contracts.review import ReviewVerdict
from module.memory_management.topic.core.domain.publication import TopicWrite, TopicWriteItem
from module.memory_management.topic.core.domain.generation import (
    TopicAction,
    TopicApplication,
    TopicBatchResult,
    TopicOperation,
    TopicOperationKind,
)
from module.memory_management.topic.core.domain.plan import TopicPlan
from module.memory_management.topic.core.domain.review import TopicApplicationReview, TopicReviewResult
from module.shared.memory.types.retention import RecallTier
from storage.sqlite.models import MemoryJobRow
from storage.sqlite.value_codecs import SQLiteValueCodec
from module.memory_management.shared.repository.document_mappers import (
    _memory_evidence_ref_from_json,
    _memory_evidence_ref_to_json,
    _memory_topic_document_from_json,
    _memory_topic_document_to_json,
    _memory_topic_entry_from_json,
    _memory_topic_entry_to_json,
)
from module.memory_management.shared.jobs.repository.job_fields import (
    _memory_job_common_from_row,
    _memory_job_common_payload_to_json,
)
from module.memory_management.object.repository.mappers import (
    _memory_object_from_json,
    _memory_object_to_json,
)


def _memory_topic_operation_to_json(value: TopicOperation) -> dict[str, Any]:
    return {
        "kind": value.kind.value,
        "object_ids": [item for item in value.object_ids],
        "evidence": [_memory_evidence_ref_to_json(item) for item in value.evidence],
        "target_entry_id": (value.target_entry_id if value.target_entry_id is not None else None),
        "proposed_entry": (_memory_topic_entry_to_json(value.proposed_entry) if value.proposed_entry is not None else None),
    }


def _memory_topic_operation_from_json(data: dict[str, Any]) -> TopicOperation:
    return TopicOperation(
        kind=TopicOperationKind(data['kind']),
        object_ids=tuple(item for item in data['object_ids']),
        evidence=tuple(_memory_evidence_ref_from_json(item) for item in data['evidence']),
        target_entry_id=(data['target_entry_id'] if data['target_entry_id'] is not None else None),
        proposed_entry=(_memory_topic_entry_from_json(data['proposed_entry']) if data['proposed_entry'] is not None else None),
    )


def _memory_topic_batch_result_to_json(value: TopicBatchResult) -> dict[str, Any]:
    return {"result_version": value.result_version,
        "applications": [_memory_topic_application_to_json(item) for item in value.applications],
        "pending_object_ids": list(value.pending_object_ids), "content_hash": value.content_hash,
        "prompt_hash": value.prompt_hash, "generated_at": SQLiteValueCodec.datetime_to_json(value.generated_at)}


def _memory_topic_batch_result_from_json(data: dict[str, Any]) -> TopicBatchResult:
    return TopicBatchResult(result_version=data["result_version"],
        applications=tuple(_memory_topic_application_from_json(item) for item in data["applications"]),
        pending_object_ids=tuple(data["pending_object_ids"]), content_hash=data["content_hash"],
        prompt_hash=data["prompt_hash"], generated_at=SQLiteValueCodec.datetime_from_json(data["generated_at"]))


def _memory_topic_review_result_to_json(value: TopicReviewResult) -> dict[str, Any]:
    return {"result_version": value.result_version, "result_hash": value.result_hash,
        "application_reviews": [{"application_id": item.application_id, "review_result": item.review_result.value,
            "issues": list(item.issues), "approved_document": _memory_topic_document_to_json(item.approved_document)
                if item.approved_document is not None else None} for item in value.application_reviews]}


def _memory_topic_review_result_from_json(data: dict[str, Any]) -> TopicReviewResult:
    return TopicReviewResult(result_version=data["result_version"], result_hash=data["result_hash"],
        application_reviews=tuple(TopicApplicationReview(application_id=item["application_id"],
            review_result=ReviewVerdict(item["review_result"]), issues=tuple(item["issues"]),
            approved_document=_memory_topic_document_from_json(item["approved_document"])
                if item["approved_document"] is not None else None) for item in data["application_reviews"]))


def _memory_topic_application_to_json(value: TopicApplication) -> dict[str, Any]:
    return {"application_id": value.application_id, "topic_id": value.topic_id, "action": value.action.value,
        "object_ids": list(value.object_ids), "document": _memory_topic_document_to_json(value.document),
        "base_ref": SQLiteValueCodec.memory_ref_to_json(value.base_ref) if value.base_ref is not None else None,
        "discussion_session_ids": list(value.discussion_session_ids),
        "operations": [_memory_topic_operation_to_json(item) for item in value.operations],
        "discussion_source_refs": [_memory_evidence_ref_to_json(item) for item in value.discussion_source_refs],
        "reason": value.reason}


def _memory_topic_application_from_json(data: dict[str, Any]) -> TopicApplication:
    return TopicApplication(application_id=data["application_id"], topic_id=data["topic_id"],
        action=TopicAction(data["action"]), object_ids=tuple(data["object_ids"]),
        document=_memory_topic_document_from_json(data["document"]),
        base_ref=SQLiteValueCodec.memory_ref_from_json(data["base_ref"]) if data["base_ref"] is not None else None,
        discussion_session_ids=tuple(data["discussion_session_ids"]),
        operations=tuple(_memory_topic_operation_from_json(item) for item in data["operations"]),
        discussion_source_refs=tuple(_memory_evidence_ref_from_json(item) for item in data["discussion_source_refs"]),
        reason=data["reason"])


def topic_job_input_to_json(value: TopicJobInput) -> dict[str, Any]:
    return {
        "character_id": value.character_id,
        "source_session_ids": [item for item in value.source_session_ids],
        "target_id": (value.target_id if value.target_id is not None else None),
        "base_revision": (value.base_revision if value.base_revision is not None else None),
        "base_content_hash": (value.base_content_hash if value.base_content_hash is not None else None),
        "target_revision": (value.target_revision if value.target_revision is not None else None),
        "recall_tier": value.recall_tier.value,
        "target_kind": (value.target_kind.value if value.target_kind is not None else None),
        "object_ids": list(value.object_ids),
        "edited_document": (_memory_topic_document_to_json(value.edited_document) if value.edited_document is not None else None),
    }


def topic_job_input_from_json(data: dict[str, Any]) -> TopicJobInput:
    return TopicJobInput(
        character_id=data['character_id'],
        source_session_ids=tuple(item for item in data['source_session_ids']),
        target_id=(data['target_id'] if data['target_id'] is not None else None),
        base_revision=(data['base_revision'] if data['base_revision'] is not None else None),
        base_content_hash=(data['base_content_hash'] if data['base_content_hash'] is not None else None),
        target_revision=(data['target_revision'] if data['target_revision'] is not None else None),
        recall_tier=RecallTier(data['recall_tier']),
        target_kind=(MemoryKind(data['target_kind']) if data['target_kind'] is not None else None),
        object_ids=tuple(data['object_ids']),
        edited_document=(_memory_topic_document_from_json(data['edited_document']) if data['edited_document'] is not None else None),
    )


def topic_plan_to_json(value: TopicPlan | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"character_id": value.character_id, "object_ids": list(value.object_ids),
        "related_object_ids": list(value.related_object_ids),
        "topic_refs": [SQLiteValueCodec.memory_ref_to_json(item) for item in value.topic_refs],
        "min_distinct_sessions": value.min_distinct_sessions}


def topic_plan_from_json(data: dict[str, Any] | None) -> TopicPlan | None:
    if data is None:
        return None
    return TopicPlan(character_id=data["character_id"], object_ids=tuple(data["object_ids"]),
        related_object_ids=tuple(data["related_object_ids"]),
        topic_refs=tuple(SQLiteValueCodec.memory_ref_from_json(item) for item in data["topic_refs"]),
        min_distinct_sessions=data["min_distinct_sessions"])


def topic_source_to_json(value: TopicSourceInput | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"plan": topic_plan_to_json(value.plan), "groups": [
        {"object": _memory_object_to_json(group.object),
         "related_objects": [_memory_object_to_json(item) for item in group.related_objects],
         "existing_topics": [{"ref": SQLiteValueCodec.memory_ref_to_json(item.ref),
                              "document": _memory_topic_document_to_json(item.document)}
                             for item in group.existing_topics]}
        for group in value.groups]}


def topic_source_from_json(data: dict[str, Any] | None) -> TopicSourceInput | None:
    if data is None:
        return None
    return TopicSourceInput(plan=topic_plan_from_json(data["plan"]), groups=tuple(
        TopicObjectGroup(_memory_object_from_json(group["object"]),
            tuple(_memory_object_from_json(item) for item in group["related_objects"]),
            tuple(TopicContext(SQLiteValueCodec.memory_ref_from_json(item["ref"]),
                _memory_topic_document_from_json(item["document"])) for item in group["existing_topics"]))
        for group in data["groups"]))


def topic_write_item_to_json(value: TopicWriteItem) -> dict[str, Any]:
    return {"application_id": value.application_id, "ref": SQLiteValueCodec.memory_ref_to_json(value.ref),
        "write": {"document": _memory_topic_document_to_json(value.write.document),
                  "content_hash": value.write.content_hash}}


def topic_write_item_from_json(data: dict[str, Any]) -> TopicWriteItem:
    write = data["write"]
    return TopicWriteItem(data["application_id"], SQLiteValueCodec.memory_ref_from_json(data["ref"]),
        TopicWrite(_memory_topic_document_from_json(write["document"]), write["content_hash"]))


def topic_job_payload_to_json(value: TopicJob) -> dict[str, Any]:
    """topic专用产物；不保存daily日期和原文计划。"""
    data = _memory_job_common_payload_to_json(value)
    data.update({
        "job_family": "topic",
        "input": topic_job_input_to_json(value.input),
        "plan": topic_plan_to_json(value.plan),
        "generation_source": topic_source_to_json(value.generation_source),
        "published_refs": ([SQLiteValueCodec.memory_ref_to_json(ref) for ref in value.published_refs]
                           if value.published_refs is not None else None),
        "write_results": ([topic_write_item_to_json(item) for item in value.write_results]
                          if value.write_results is not None else None),
        "generation_result": (_memory_topic_batch_result_to_json(value.generation_result) if value.generation_result is not None else None),
        "review": (_memory_topic_review_result_to_json(value.review) if value.review is not None else None),
    })
    return data


def topic_job_to_domain(row: MemoryJobRow) -> TopicJob:
    """从共用任务表还原TopicJob，不承载object提取任务。"""
    common = _memory_job_common_from_row(row)
    data = row.payload_json
    if data["job_family"] != "topic":
        raise ValueError("memory_record job family mismatch")
    return TopicJob(
        **common,
        input=topic_job_input_from_json(data["input"]),
        plan=topic_plan_from_json(data["plan"]),
        generation_source=topic_source_from_json(data.get("generation_source")),
        published_refs=(tuple(SQLiteValueCodec.memory_ref_from_json(ref) for ref in data["published_refs"])
                        if data.get("published_refs") is not None else None),
        write_results=(tuple(topic_write_item_from_json(item) for item in data["write_results"])
                       if data.get("write_results") is not None else None),
        generation_result=(_memory_topic_batch_result_from_json(data['generation_result']) if data['generation_result'] is not None else None),
        review=(_memory_topic_review_result_from_json(data['review']) if data['review'] is not None else None),
    )
