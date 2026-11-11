"""正式 Daily 与 Topic 版本及正文的持久化映射。"""

from datetime import date, datetime
from typing import Any
from module.shared.common.errors.base import InvalidStateError
from module.shared.memory.types.references import MemoryKind
from module.memory_management.daily.core.domain.document import DailyDocument
from module.memory_management.topic.core.domain.document import (
    TopicCurrentState,
    TopicDocument,
    TopicEntry,
    TopicIdentity,
)
from module.shared.memory.types.references import EvidenceRef
from module.shared.memory.types.retention import RecallTier
from module.memory_management.shared.core.domain.document import DailyMemory, TopicMemory
from module.shared.common.time import BackendTime
from storage.sqlite.models import DailyMemoryRow, TopicMemoryRow


def daily_memory_to_row(
    value: DailyMemory, *, character_id: str,
    recall_tier: RecallTier = RecallTier.HOT, created_at: datetime | None = None,
) -> DailyMemoryRow:
    """创建包含正式正文的daily行；角色和池状态由存储调用方提供。

    已有记录更新时逐项修改已加载的行，避免覆盖创建时间等存储字段。
    """
    if value.kind != MemoryKind.DAILY:
        raise ValueError("DailyMemory必须使用daily类型")
    return DailyMemoryRow(
        daily_id=value.document_id, character_id=character_id,
        memory_date=value.memory_date, source_session_ids_json=list(value.source_session_ids),
        document_json=_memory_daily_document_to_json(value.document),
        revision=value.current_revision, content_hash=value.content_hash,
        vector_document_key=value.vector_document_key, published_at=value.published_at,
        recall_tier=recall_tier.value,
        last_mentioned_at=BackendTime.local(value.published_at).date(),
        created_at=value.published_at if created_at is None else created_at,
        updated_at=value.updated_at,
    )


def daily_memory_to_domain(row: DailyMemoryRow) -> DailyMemory:
    """还原正式daily版本与完整正文，不执行切版。"""
    return DailyMemory(
        document_id=row.daily_id, kind=MemoryKind.DAILY, current_revision=row.revision,
        content_hash=row.content_hash, vector_document_key=row.vector_document_key,
        published_at=row.published_at, updated_at=row.updated_at,
        memory_date=row.memory_date, source_session_ids=tuple(row.source_session_ids_json),
        document=_memory_daily_document_from_json(row.document_json),
    )


def topic_memory_to_row(value: TopicMemory, *, character_id: str,
                        recall_tier: RecallTier = RecallTier.HOT,
                        created_at: datetime | None = None) -> TopicMemoryRow:
    """只构造新行；更新时显式修改已加载行，避免覆盖创建时间等存储元数据。"""
    if value.kind != MemoryKind.TOPIC:
        raise ValueError("TopicMemory必须使用topic类型")
    return TopicMemoryRow(
        topic_id=value.document_id, character_id=character_id,
        revision=value.current_revision, content_hash=value.content_hash,
        document_json=_memory_topic_document_to_json(value.document),
        source_refs_json=[_memory_evidence_ref_to_json(item) for item in value.source_refs],
        vector_document_key=value.vector_document_key, published_at=value.published_at,
        recall_tier=recall_tier.value,
        last_mentioned_at=BackendTime.local(value.published_at).date(),
        created_at=value.published_at if created_at is None else created_at, updated_at=value.updated_at,
    )


def topic_memory_to_domain(row: TopicMemoryRow) -> TopicMemory:
    """从SQLite还原正式正文；元数据不完整时明确拒绝，不回退到Qdrant或伪造空档案。"""
    if row.document_json is None or row.vector_document_key is None or row.published_at is None:
        raise InvalidStateError("正式topic缺少正文、向量版本键或发布时间，需要补齐后读取")
    return TopicMemory(
        document_id=row.topic_id, kind=MemoryKind.TOPIC, current_revision=row.revision,
        content_hash=row.content_hash, vector_document_key=row.vector_document_key,
        published_at=row.published_at, updated_at=row.updated_at,
        source_refs=tuple(_memory_evidence_ref_from_json(item) for item in row.source_refs_json),
        document=_memory_topic_document_from_json(row.document_json),
    )


def _memory_evidence_ref_to_json(value: EvidenceRef) -> dict[str, Any]:
    return {
        "session_id": value.session_id,
        "message_id": value.message_id,
        "quote": value.quote,
        "turn_id": (value.turn_id if value.turn_id is not None else None),
        "source_revision": (value.source_revision if value.source_revision is not None else None),
        "source_hash": (value.source_hash if value.source_hash is not None else None),
    }


def _memory_evidence_ref_from_json(data: dict[str, Any]) -> EvidenceRef:
    return EvidenceRef(
        session_id=data['session_id'],
        message_id=data['message_id'],
        quote=data['quote'],
        turn_id=(data['turn_id'] if data['turn_id'] is not None else None),
        source_revision=(data['source_revision'] if data['source_revision'] is not None else None),
        source_hash=(data['source_hash'] if data['source_hash'] is not None else None),
    )


def _memory_topic_document_to_json(value: TopicDocument) -> dict[str, Any]:
    return value.to_data()


def _memory_topic_document_from_json(data: dict[str, Any]) -> TopicDocument:
    return TopicDocument(
        topic=data['topic'],
        topic_identity=_memory_topic_identity_from_json(data['topic_identity']),
        current_state=_memory_topic_current_state_from_json(data['current_state']),
        entries=tuple(_memory_topic_entry_from_json(item) for item in data['entries']),
    )


def _memory_topic_identity_to_json(value: TopicIdentity) -> dict[str, Any]:
    return {
        "description": value.description,
        "source_refs": [_memory_evidence_ref_to_json(item) for item in value.source_refs],
    }


def _memory_topic_identity_from_json(data: dict[str, Any]) -> TopicIdentity:
    return TopicIdentity(
        description=data['description'],
        source_refs=tuple(_memory_evidence_ref_from_json(item) for item in data['source_refs']),
    )


def _memory_topic_current_state_to_json(value: TopicCurrentState) -> dict[str, Any]:
    return {
        "text": value.text,
        "source_entry_ids": [item for item in value.source_entry_ids],
    }


def _memory_topic_current_state_from_json(data: dict[str, Any]) -> TopicCurrentState:
    return TopicCurrentState(
        text=data['text'],
        source_entry_ids=tuple(item for item in data['source_entry_ids']),
    )


def _memory_topic_entry_to_json(value: TopicEntry) -> dict[str, Any]:
    return {
        "entry_id": value.entry_id,
        "statement": value.statement,
        "occurrence_time": value.occurrence_time.isoformat() if value.occurrence_time is not None else None,
        "acquire_time": value.acquire_time.isoformat(),
        "kind": value.kind, "actor": value.actor,
        "target_time": value.target_time.isoformat() if value.target_time is not None else None,
        "source_refs": [_memory_evidence_ref_to_json(item) for item in value.source_refs],
        "corrects_entry_id": (value.corrects_entry_id if value.corrects_entry_id is not None else None),
    }


def _memory_topic_entry_from_json(data: dict[str, Any]) -> TopicEntry:
    return TopicEntry(
        entry_id=data['entry_id'],
        statement=data['statement'],
        occurrence_time=date.fromisoformat(data['occurrence_time']) if data['occurrence_time'] is not None else None,
        acquire_time=date.fromisoformat(data['acquire_time']),
        kind=data['kind'], actor=data['actor'],
        target_time=date.fromisoformat(data['target_time']) if data['target_time'] is not None else None,
        source_refs=tuple(_memory_evidence_ref_from_json(item) for item in data['source_refs']),
        corrects_entry_id=(data['corrects_entry_id'] if data['corrects_entry_id'] is not None else None),
    )


def _memory_daily_document_to_json(value: DailyDocument) -> dict[str, Any]:
    return value.to_data()


def _memory_daily_document_from_json(data: dict[str, Any]) -> DailyDocument:
    return DailyDocument.from_data(data)


def _memory_document_to_json(value: DailyDocument | TopicDocument) -> dict[str, Any]:
    if isinstance(value, DailyDocument):
        return {"type": "daily_document", "value": _memory_daily_document_to_json(value)}
    if isinstance(value, TopicDocument):
        return {"type": "topic_document", "value": _memory_topic_document_to_json(value)}
    raise ValueError("unsupported memory_record payload variant")


def _memory_document_from_json(data: dict[str, Any]) -> DailyDocument | TopicDocument:
    if data["type"] == "daily_document":
        return _memory_daily_document_from_json(data["value"])
    if data["type"] == "topic_document":
        return _memory_topic_document_from_json(data["value"])
    raise ValueError("unsupported memory_record payload variant")
