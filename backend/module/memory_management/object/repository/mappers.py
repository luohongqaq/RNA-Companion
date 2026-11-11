"""记忆对象、事件和对象任务的持久化映射。"""

from datetime import date
from typing import Any
from module.memory_management.object.core.domain.job import ObjectJob, ObjectJobInput
from module.memory_management.object.core.domain.document import (
    ObjectState,
    MemoryObject,
    ObjectEvent,
    ObjectPlan,
)
from module.memory_management.object.core.domain.generation import ObjectSelectionNote
from module.shared.memory.types.retention import RecallTier
from storage.sqlite.models import MemoryJobRow, MemoryObjectRow
from storage.sqlite.value_codecs import SQLiteValueCodec
from module.memory_management.shared.repository.document_mappers import (
    _memory_evidence_ref_from_json,
    _memory_evidence_ref_to_json,
)
from module.memory_management.shared.jobs.repository.job_fields import (
    _memory_job_common_from_row,
    _memory_job_common_payload_to_json,
)


def _memory_object_event_to_json(value: ObjectEvent) -> dict[str, Any]:
    return {
        "actor": value.actor,
        "statement": value.statement,
        "acquire_time": value.acquire_time.isoformat(),
        "occurrence_time": value.occurrence_time.isoformat() if value.occurrence_time is not None else None,
        "source_refs": [_memory_evidence_ref_to_json(item) for item in value.source_refs],
    }


def _memory_object_event_from_json(data: dict[str, Any]) -> ObjectEvent:
    return ObjectEvent(
        actor=data["actor"], statement=data["statement"],
        acquire_time=date.fromisoformat(data["acquire_time"]),
        occurrence_time=date.fromisoformat(data["occurrence_time"]) if data["occurrence_time"] is not None else None,
        source_refs=tuple(_memory_evidence_ref_from_json(item) for item in data["source_refs"]),
    )


def _memory_object_plan_to_json(value: ObjectPlan) -> dict[str, Any]:
    return {
        "actor": value.actor,
        "statement": value.statement,
        "acquire_time": value.acquire_time.isoformat(),
        "target_time": value.target_time.isoformat() if value.target_time is not None else None,
        "source_refs": [_memory_evidence_ref_to_json(item) for item in value.source_refs],
    }


def _memory_object_plan_from_json(data: dict[str, Any]) -> ObjectPlan:
    return ObjectPlan(
        actor=data["actor"], statement=data["statement"],
        acquire_time=date.fromisoformat(data["acquire_time"]),
        target_time=date.fromisoformat(data["target_time"]) if data["target_time"] is not None else None,
        source_refs=tuple(_memory_evidence_ref_from_json(item) for item in data["source_refs"]),
    )


def _memory_object_selection_note_to_json(value: ObjectSelectionNote) -> dict[str, Any]:
    return {"item": value.item, "decision": value.decision, "reason": value.reason,
            "message_ids": list(value.message_ids)}


def _memory_object_selection_note_from_json(data: dict[str, Any]) -> ObjectSelectionNote:
    return ObjectSelectionNote(item=data["item"], decision=data["decision"], reason=data["reason"],
                               message_ids=tuple(data["message_ids"]))


def _memory_object_to_json(value: MemoryObject) -> dict[str, Any]:
    return {
        "object_id": value.object_id,
        "character_id": value.character_id,
        "object": value.object,
        "events": [_memory_object_event_to_json(item) for item in value.events],
        "plans": [_memory_object_plan_to_json(item) for item in value.plans],
        "source_position_key": value.source_position_key,
        "created_at": SQLiteValueCodec.datetime_to_json(value.created_at),
        "forgetfulness": value.forgetfulness,
        "state": value.state.value,
        "recall_tier": value.recall_tier.value,
        "linked_topic_id": (value.linked_topic_id if value.linked_topic_id is not None else None),
    }


def _memory_object_from_json(data: dict[str, Any]) -> MemoryObject:
    return MemoryObject(
        object_id=data['object_id'],
        character_id=data['character_id'],
        object=data["object"],
        events=tuple(_memory_object_event_from_json(item) for item in data["events"]),
        plans=tuple(_memory_object_plan_from_json(item) for item in data["plans"]),
        source_position_key=data['source_position_key'],
        created_at=SQLiteValueCodec.datetime_from_json(data['created_at']),
        forgetfulness=data['forgetfulness'],
        state=ObjectState(data['state']),
        recall_tier=RecallTier(data['recall_tier']),
        linked_topic_id=(data['linked_topic_id'] if data['linked_topic_id'] is not None else None),
    )


def memory_object_to_row(value: MemoryObject) -> MemoryObjectRow:
    """构造新的正式object行；复用任务候选的事件、计划JSON格式。"""
    return MemoryObjectRow(
        object_id=value.object_id, character_id=value.character_id, object_name=value.object,
        events_json=[_memory_object_event_to_json(item) for item in value.events],
        plans_json=[_memory_object_plan_to_json(item) for item in value.plans],
        source_position_key=value.source_position_key, forgetfulness=value.forgetfulness,
        object_state=value.state.value, recall_tier=value.recall_tier.value,
        linked_topic_id=value.linked_topic_id, created_at=value.created_at,
    )


def memory_object_to_domain(row: MemoryObjectRow) -> MemoryObject:
    """读取正式object，不执行状态迁移或外部I/O。"""
    return MemoryObject(
        object_id=row.object_id, character_id=row.character_id, object=row.object_name,
        events=tuple(_memory_object_event_from_json(item) for item in row.events_json),
        plans=tuple(_memory_object_plan_from_json(item) for item in row.plans_json),
        source_position_key=row.source_position_key, forgetfulness=row.forgetfulness,
        state=ObjectState(row.object_state), recall_tier=RecallTier(row.recall_tier),
        linked_topic_id=row.linked_topic_id, created_at=row.created_at,
    )


def object_job_input_to_json(value: ObjectJobInput) -> dict[str, Any]:
    return {"character_id": value.character_id, "source_session_ids": list(value.source_session_ids),
        "source_message_ids": list(value.source_message_ids), "target_id": value.target_id,
        "base_revision": value.base_revision, "base_content_hash": value.base_content_hash,
        "target_revision": value.target_revision, "recall_tier": value.recall_tier.value}


def object_job_input_from_json(data: dict[str, Any]) -> ObjectJobInput:
    return ObjectJobInput(character_id=data["character_id"], source_session_ids=tuple(data["source_session_ids"]),
        source_message_ids=tuple(data["source_message_ids"]), target_id=data["target_id"],
        base_revision=data["base_revision"], base_content_hash=data["base_content_hash"],
        target_revision=data["target_revision"], recall_tier=RecallTier(data["recall_tier"]))


def object_job_payload_to_json(value: ObjectJob) -> dict[str, Any]:
    data = _memory_job_common_payload_to_json(value)
    data.update(job_family="object", input=object_job_input_to_json(value.input),
        objects=[_memory_object_to_json(item) for item in value.objects],
        selection_notes=[_memory_object_selection_note_to_json(item) for item in value.selection_notes])
    return data


def object_job_to_domain(row: MemoryJobRow) -> ObjectJob:
    data = row.payload_json
    if data["job_family"] != "object":
        raise ValueError("memory job family mismatch")
    return ObjectJob(**_memory_job_common_from_row(row),
        input=object_job_input_from_json(data["input"]),
        objects=tuple(_memory_object_from_json(item) for item in data["objects"]),
        selection_notes=tuple(_memory_object_selection_note_from_json(item) for item in data["selection_notes"]))
