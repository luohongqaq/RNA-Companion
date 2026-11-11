"""日记忆任务、候选、审核和计划的持久化映射。"""

from datetime import date
from typing import Any
from module.shared.memory.types.references import MemoryKind
from module.memory_management.daily.core.domain.generation import DailyCandidate
from module.memory_management.daily.core.domain.plan import DailyPlan
from module.memory_management.daily.core.domain.review import DailyReview
from module.memory_management.daily.core.domain.publication import DailyWrite
from module.memory_management.daily.core.domain.job import DailyJob, DailyJobInput
from module.memory_management.daily.core.domain.review import DailyIssueType, ReviewIssue
from module.memory_management.shared.contracts.review import ReviewSeverity, ReviewVerdict
from module.shared.memory.types.retention import RecallTier
from storage.sqlite.models import MemoryJobRow
from storage.sqlite.value_codecs import SQLiteValueCodec
from module.memory_management.shared.repository.document_mappers import (
    _memory_daily_document_from_json,
    _memory_daily_document_to_json,
)
from module.memory_management.shared.jobs.repository.job_fields import (
    _memory_job_common_from_row,
    _memory_job_common_payload_to_json,
)


def _memory_daily_candidate_to_json(value: DailyCandidate) -> dict[str, Any]:
    return {
        "candidate_version": value.candidate_version,
        "document": _memory_daily_document_to_json(value.document),
        "content_hash": value.content_hash,
        "generated_at": SQLiteValueCodec.datetime_to_json(value.generated_at),
    }


def _memory_daily_candidate_from_json(data: dict[str, Any]) -> DailyCandidate:
    return DailyCandidate(
        candidate_version=data['candidate_version'],
        document=_memory_daily_document_from_json(data['document']),
        content_hash=data['content_hash'],
        generated_at=SQLiteValueCodec.datetime_from_json(data['generated_at']),
    )


def _memory_daily_review_to_json(value: DailyReview) -> dict[str, Any]:
    return {
        "review_result": value.review_result.value,
        "issues": [_memory_review_issue_to_json(item) for item in value.issues],
        "approved_daily": (_memory_daily_document_to_json(value.approved_daily) if value.approved_daily is not None else None),
        "review_summary": value.review_summary,
    }


def _memory_daily_review_from_json(data: dict[str, Any]) -> DailyReview:
    return DailyReview(
        review_result=ReviewVerdict(data['review_result']),
        issues=tuple(_memory_review_issue_from_json(item) for item in data['issues']),
        approved_daily=(_memory_daily_document_from_json(data['approved_daily']) if data['approved_daily'] is not None else None),
        review_summary=data['review_summary'],
    )


def _memory_review_issue_to_json(value: ReviewIssue) -> dict[str, Any]:
    return {
        "severity": value.severity.value,
        "type": value.type.value,
        "field": value.field,
        "description": value.description,
        "source_turn_ids": [item for item in value.source_turn_ids],
    }


def _memory_review_issue_from_json(data: dict[str, Any]) -> ReviewIssue:
    return ReviewIssue(
        severity=ReviewSeverity(data['severity']),
        type=DailyIssueType(data['type']),
        field=data['field'],
        description=data['description'],
        source_turn_ids=tuple(item for item in data['source_turn_ids']),
    )


def daily_plan_to_json(value: DailyPlan | None) -> dict[str, Any] | None:
    """只编码范围及固定版本，不保存模型输入正文。"""
    if value is None:
        return None
    return {
        "session_id": value.session_id,
        "character_id": value.character_id,
        "memory_date": value.memory_date.isoformat(),
        "source_turns": SQLiteValueCodec.range_to_json(value.source_turns),
        "context_only_turns": SQLiteValueCodec.range_to_json(value.context_only_turns) if value.context_only_turns is not None else None,
        "base_daily_ref": SQLiteValueCodec.memory_ref_to_json(value.base_daily_ref) if value.base_daily_ref is not None else None,
        "inherited_context": SQLiteValueCodec.inherited_context_to_json(value.inherited_context),
    }


def daily_plan_from_json(data: dict[str, Any] | None) -> DailyPlan | None:
    """按当前结构还原计划；不查询最新版daily或继承摘要。"""
    if data is None:
        return None
    return DailyPlan(
        session_id=data["session_id"], character_id=data["character_id"],
        memory_date=date.fromisoformat(data["memory_date"]),
        source_turns=SQLiteValueCodec.range_from_json(data["source_turns"]),
        context_only_turns=SQLiteValueCodec.range_from_json(data["context_only_turns"]) if data["context_only_turns"] is not None else None,
        base_daily_ref=SQLiteValueCodec.memory_ref_from_json(data["base_daily_ref"]) if data["base_daily_ref"] is not None else None,
        inherited_context=SQLiteValueCodec.inherited_context_from_json(data["inherited_context"]),
    )


def daily_job_input_to_json(value: DailyJobInput) -> dict[str, Any]:
    return {
        "character_id": value.character_id,
        "source_session_ids": [item for item in value.source_session_ids],
        "target_id": (value.target_id if value.target_id is not None else None),
        "base_revision": (value.base_revision if value.base_revision is not None else None),
        "base_content_hash": (value.base_content_hash if value.base_content_hash is not None else None),
        "target_revision": (value.target_revision if value.target_revision is not None else None),
        "recall_tier": value.recall_tier.value,
        "source_message_ids": [item for item in value.source_message_ids],
        "target_kind": (value.target_kind.value if value.target_kind is not None else None),
        "edited_document": (_memory_daily_document_to_json(value.edited_document) if value.edited_document is not None else None),
        "memory_date": (value.memory_date.isoformat() if value.memory_date is not None else None),
    }


def daily_job_input_from_json(data: dict[str, Any]) -> DailyJobInput:
    return DailyJobInput(
        character_id=data['character_id'],
        source_session_ids=tuple(item for item in data['source_session_ids']),
        target_id=(data['target_id'] if data['target_id'] is not None else None),
        base_revision=(data['base_revision'] if data['base_revision'] is not None else None),
        base_content_hash=(data['base_content_hash'] if data['base_content_hash'] is not None else None),
        target_revision=(data['target_revision'] if data['target_revision'] is not None else None),
        recall_tier=RecallTier(data['recall_tier']),
        source_message_ids=tuple(item for item in data['source_message_ids']),
        target_kind=(MemoryKind(data['target_kind']) if data['target_kind'] is not None else None),
        edited_document=(_memory_daily_document_from_json(data['edited_document']) if data['edited_document'] is not None else None),
        memory_date=(date.fromisoformat(data['memory_date']) if data['memory_date'] is not None else None),
    )


def daily_job_payload_to_json(value: DailyJob) -> dict[str, Any]:
    """daily专用输入和产物；不混入topic申请或object列表。"""
    data = _memory_job_common_payload_to_json(value)
    data.update({
        "job_family": "daily",
        "input": daily_job_input_to_json(value.input),
        "plan": (daily_plan_to_json(value.plan) if value.plan is not None else None),
        "candidate": (_memory_daily_candidate_to_json(value.candidate) if value.candidate is not None else None),
        "review": (_memory_daily_review_to_json(value.review) if value.review is not None else None),
        "reextraction_count": value.reextraction_count,
        "write_result": ({"document": _memory_daily_document_to_json(value.write_result.document),
                          "content_hash": value.write_result.content_hash} if value.write_result is not None else None),
    })
    return data


def daily_job_to_domain(row: MemoryJobRow) -> DailyJob:
    """从共用任务表还原DailyJob，任务类型与输入类型必须一致。"""
    common = _memory_job_common_from_row(row)
    data = row.payload_json
    if data["job_family"] != "daily":
        raise ValueError("memory_record job family mismatch")
    return DailyJob(
        **common,
        input=daily_job_input_from_json(data["input"]),
        plan=(daily_plan_from_json(data['plan']) if data['plan'] is not None else None),
        candidate=(_memory_daily_candidate_from_json(data['candidate']) if data['candidate'] is not None else None),
        review=(_memory_daily_review_from_json(data['review']) if data['review'] is not None else None),
        reextraction_count=data['reextraction_count'],
        write_result=(DailyWrite(_memory_daily_document_from_json(data["write_result"]["document"]),
            data["write_result"]["content_hash"])
            if data["write_result"] is not None else None),
    )
