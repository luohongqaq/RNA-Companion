"""记忆任务共用字段、来源清单和索引材料的持久化映射。"""

from datetime import date
from typing import Any
from module.memory_management.shared.jobs.core.domain.job import MemoryJob, MemoryJobStatus, MemoryJobType
from module.memory_management.shared.contracts.source import MemorySourceManifest
from module.shared.memory.types.indexing import PersonRef
from module.memory_management.shared.core.domain.document import VectorDocument
from storage.sqlite.models import MemoryJobRow
from storage.sqlite.value_codecs import SQLiteValueCodec
from module.memory_management.shared.repository.document_mappers import (
    _memory_document_from_json,
    _memory_document_to_json,
)


def _memory_vector_document_to_json(value: VectorDocument) -> dict[str, Any]:
    return {
        "vector_document_key": value.vector_document_key,
        "ref": SQLiteValueCodec.memory_ref_to_json(value.ref),
        "character_id": value.character_id,
        "document": _memory_document_to_json(value.document),
        "people": [_memory_person_ref_to_json(item) for item in value.people],
        "topics": [item for item in value.topics],
        "preview_text": value.preview_text,
        "session_started_at": (SQLiteValueCodec.datetime_to_json(value.session_started_at) if value.session_started_at is not None else None),
        "session_ended_at": (SQLiteValueCodec.datetime_to_json(value.session_ended_at) if value.session_ended_at is not None else None),
    }


def _memory_vector_document_from_json(data: dict[str, Any]) -> VectorDocument:
    return VectorDocument(
        vector_document_key=data['vector_document_key'],
        ref=SQLiteValueCodec.memory_ref_from_json(data['ref']),
        character_id=data['character_id'],
        document=_memory_document_from_json(data['document']),
        people=tuple(_memory_person_ref_from_json(item) for item in data['people']),
        topics=tuple(item for item in data['topics']),
        preview_text=data['preview_text'],
        session_started_at=(SQLiteValueCodec.datetime_from_json(data['session_started_at']) if data['session_started_at'] is not None else None),
        session_ended_at=(SQLiteValueCodec.datetime_from_json(data['session_ended_at']) if data['session_ended_at'] is not None else None),
    )


def _memory_person_ref_to_json(value: PersonRef) -> dict[str, Any]:
    return {
        "name": value.name,
        "person_id": (value.person_id if value.person_id is not None else None),
    }


def _memory_person_ref_from_json(data: dict[str, Any]) -> PersonRef:
    return PersonRef(
        name=data['name'],
        person_id=(data['person_id'] if data['person_id'] is not None else None),
    )


def memory_source_manifest_to_json(value: MemorySourceManifest) -> dict[str, Any]:
    return {
        "character_id": value.character_id,
        "source_session_ids": [item for item in value.source_session_ids],
        "source_message_ids": [item for item in value.source_message_ids],
        "source_content_hash": value.source_content_hash,
        "memory_date": (value.memory_date.isoformat() if value.memory_date is not None else None),
        "source_daily_refs": [SQLiteValueCodec.memory_ref_to_json(item) for item in value.source_daily_refs],
        "base_ref": (SQLiteValueCodec.memory_ref_to_json(value.base_ref) if value.base_ref is not None else None),
    }


def memory_source_manifest_from_json(data: dict[str, Any]) -> MemorySourceManifest:
    return MemorySourceManifest(
        character_id=data['character_id'],
        source_session_ids=tuple(item for item in data['source_session_ids']),
        source_message_ids=tuple(item for item in data['source_message_ids']),
        source_content_hash=data['source_content_hash'],
        memory_date=(date.fromisoformat(data['memory_date']) if data['memory_date'] is not None else None),
        source_daily_refs=tuple(SQLiteValueCodec.memory_ref_from_json(item) for item in data['source_daily_refs']),
        base_ref=(SQLiteValueCodec.memory_ref_from_json(data['base_ref']) if data['base_ref'] is not None else None),
    )


def _memory_job_common_payload_to_json(value: MemoryJob) -> dict[str, Any]:
    if type(value.schema_version) is not int or value.schema_version != 6:
        raise ValueError("unsupported memory_record job payload schema")
    return {
        "schema_version": value.schema_version,
        "vector_document": (_memory_vector_document_to_json(value.vector_document) if value.vector_document is not None else None),
        "storage_job_id": (value.storage_job_id if value.storage_job_id is not None else None),
        "result": (SQLiteValueCodec.memory_ref_to_json(value.result) if value.result is not None else None),
        "last_error": (value.last_error if value.last_error is not None else None),
        "finished_at": (SQLiteValueCodec.datetime_to_json(value.finished_at) if value.finished_at is not None else None),
        "reviewed_version": (value.reviewed_version if value.reviewed_version is not None else None),
        "reviewed_hash": (value.reviewed_hash if value.reviewed_hash is not None else None),
        "applied_revision": (value.applied_revision if value.applied_revision is not None else None),
        "error_code": (value.error_code if value.error_code is not None else None),
        "attempt_version": value.attempt_version,
        "attempt_count": value.attempt_count,
        "source_manifest": (memory_source_manifest_to_json(value.source_manifest) if value.source_manifest is not None else None),
    }


def _memory_job_common_from_row(row: MemoryJobRow) -> dict[str, Any]:
    data = row.payload_json
    if not isinstance(data, dict):
        raise ValueError("memory_record job payload must be a JSON object")
    if type(data["schema_version"]) is not int or data["schema_version"] != 6:
        raise ValueError("unsupported memory_record job payload schema")
    return {
        "job_id": row.job_id,
        "job_type": MemoryJobType(row.job_type),
        "target_key": row.target_key,
        "dedupe_key": row.dedupe_key,
        "priority": row.priority,
        "status": MemoryJobStatus(row.status),
        "retry_count": row.retry_count,
        "available_at": row.available_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "schema_version": data['schema_version'],
        "vector_document": (_memory_vector_document_from_json(data['vector_document']) if data['vector_document'] is not None else None),
        "storage_job_id": (data['storage_job_id'] if data['storage_job_id'] is not None else None),
        "result": (SQLiteValueCodec.memory_ref_from_json(data['result']) if data['result'] is not None else None),
        "last_error": (data['last_error'] if data['last_error'] is not None else None),
        "finished_at": (SQLiteValueCodec.datetime_from_json(data['finished_at']) if data['finished_at'] is not None else None),
        "reviewed_version": (data['reviewed_version'] if data['reviewed_version'] is not None else None),
        "reviewed_hash": (data['reviewed_hash'] if data['reviewed_hash'] is not None else None),
        "applied_revision": (data['applied_revision'] if data['applied_revision'] is not None else None),
        "error_code": (data['error_code'] if data['error_code'] is not None else None),
        "attempt_version": data['attempt_version'],
        "attempt_count": data['attempt_count'],
        "source_manifest": (memory_source_manifest_from_json(data['source_manifest']) if data['source_manifest'] is not None else None),
    }
