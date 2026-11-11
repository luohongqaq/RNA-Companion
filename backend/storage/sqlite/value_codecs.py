"""跨仓储共用的时间、上下文和记忆引用 JSON 转换。"""

from datetime import datetime
from typing import Any
from module.shared.context.types.turns import TurnRange
from module.shared.common.time import BackendTime
from module.shared.context.types.references import InheritedContextRef, SnapshotRef
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef


class SQLiteValueCodec:
    """SQLite JSON conversions shared by repositories."""

    @staticmethod
    def datetime_to_json(value: datetime) -> str:
        """把带时区的时间转成UTC的ISO8601文本。"""
        return BackendTime.utc(value).isoformat()

    @staticmethod
    def datetime_from_json(value: str) -> datetime:
        """把ISO8601文本还原成UTC时间；缺时区直接报错。"""
        return BackendTime.utc(datetime.fromisoformat(value))

    @staticmethod
    def range_to_json(value: TurnRange) -> dict[str, Any]:
        """把轮次范围转成JSON对象。"""
        return {"session_id": value.session_id, "from_seq": value.from_seq, "through_seq": value.through_seq}

    @staticmethod
    def range_from_json(data: dict[str, Any]) -> TurnRange:
        """把JSON对象还原成轮次范围。"""
        return TurnRange(session_id=data["session_id"], from_seq=data["from_seq"], through_seq=data["through_seq"])

    @staticmethod
    def snapshot_to_json(value: SnapshotRef | None) -> dict[str, Any] | None:
        """把快照引用转成JSON对象。"""
        if value is None:
            return None
        return {
            "snapshot_id": value.snapshot_id, "content_hash": value.content_hash,
            "covered_through_turn_seq": value.covered_through_turn_seq,
        }

    @staticmethod
    def snapshot_from_json(data: dict[str, Any] | None) -> SnapshotRef | None:
        """把JSON对象还原成快照引用。"""
        if data is None:
            return None
        return SnapshotRef(snapshot_id=data["snapshot_id"],
                           content_hash=data["content_hash"], covered_through_turn_seq=data["covered_through_turn_seq"])

    @staticmethod
    def inherited_context_to_json(value: InheritedContextRef | None) -> dict[str, Any] | None:
        """把继承上下文引用转成JSON对象。"""
        if value is None:
            return None
        return {
            "predecessor_session_id": value.predecessor_session_id,
            "source_range": SQLiteValueCodec.range_to_json(value.source_range) if value.source_range is not None else None,
            "source_hash": value.source_hash,
            "summary_snapshot_ref": SQLiteValueCodec.snapshot_to_json(value.summary_snapshot_ref),
        }

    @staticmethod
    def inherited_context_from_json(data: dict[str, Any] | None) -> InheritedContextRef | None:
        """把JSON对象还原成继承上下文引用。"""
        if data is None:
            return None
        return InheritedContextRef(
            predecessor_session_id=data["predecessor_session_id"],
            source_range=SQLiteValueCodec.range_from_json(data["source_range"]) if data["source_range"] is not None else None,
            source_hash=data["source_hash"], summary_snapshot_ref=SQLiteValueCodec.snapshot_from_json(data["summary_snapshot_ref"]),
        )

    @staticmethod
    def memory_ref_to_json(value: MemoryRevisionRef) -> dict[str, Any]:
        """把记忆修订引用转成JSON对象。"""
        return {"kind": value.kind.value, "memory_id": value.memory_id,
                "revision": value.revision, "content_hash": value.content_hash}

    @staticmethod
    def memory_ref_from_json(data: dict[str, Any]) -> MemoryRevisionRef:
        """把JSON对象还原成记忆修订引用。"""
        return MemoryRevisionRef(kind=MemoryKind(data["kind"]), memory_id=data["memory_id"],
                                 revision=data["revision"], content_hash=data["content_hash"])
