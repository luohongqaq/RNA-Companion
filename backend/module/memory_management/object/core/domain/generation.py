"""Object 提取结果、取舍说明及模型输出校验。

ObjectModelOutput 解析模型返回的事件和计划，根据来源消息回填证据，并构造带稳定身份的 MemoryObject。
ObjectSelectionNote 保存保留、附着和排除的说明，仅随任务记录；正式正文不包含这些调试说明。
"""
from __future__ import annotations
from module.shared.common.errors.base import InvalidStateError
from module.shared.memory.types.references import EvidenceRef
from dataclasses import dataclass
from datetime import date, datetime, timezone
from module.memory_management.object.core.domain.document import MemoryObject, ObjectEvent, ObjectPlan
from module.memory_management.object.core.domain.source import ObjectSourceInput
from typing import Literal
from uuid import NAMESPACE_URL, uuid5


@dataclass(frozen=True, slots=True)
class ObjectSelectionNote:
    """模型给出的简短取舍说明，只随任务保存，不属于正式候选正文。"""
    item: str
    decision: Literal["keep", "attach", "exclude"]
    reason: str
    message_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.decision not in ("keep", "attach", "exclude"):
            raise ValueError("object取舍说明的decision不合法")


@dataclass(frozen=True, slots=True)
class ObjectModelOutput:
    """Object提取模型的输出契约。"""
    objects: tuple[MemoryObject, ...]
    selection_notes: tuple[ObjectSelectionNote, ...] = ()

    @classmethod
    def from_model_data(cls, data: dict, *, source: ObjectSourceInput) -> "ObjectModelOutput":
        """模型边界解析；按消息ID回填证据，不要求模型逐字复制原文。"""
        try:
            if data["session_id"] != source.session_id:
                raise ValueError("session_id与来源不一致")
            if not isinstance(data["objects"], list) or not isinstance(data["selection_notes"], list):
                raise ValueError("objects与selection_notes必须是数组")
            messages = {message.message_id: (turn.turn_id, message)
                        for turn in source.owned_turns
                        for message in (*turn.user_messages, turn.accepted_reply)}
            now = datetime.now(timezone.utc)
            objects = []
            for index, item in enumerate(data["objects"]):
                if not isinstance(item["events"], list) or not isinstance(item["plans"], list):
                    raise ValueError("events与plans必须是数组")
                position = f"extract_objects:{source.character_id}:{source.session_id}:{index}"
                events = tuple(ObjectEvent(
                    actor=entry["actor"], statement=entry["statement"],
                    acquire_time=date.fromisoformat(entry["acquire_time"]),
                    occurrence_time=date.fromisoformat(entry["occurrence_time"]) if entry["occurrence_time"] is not None else None,
                    source_refs=cls._source_refs(entry["message_ids"], messages, source.session_id),
                ) for entry in item["events"])
                plans = tuple(ObjectPlan(
                    actor=entry["actor"], statement=entry["statement"],
                    acquire_time=date.fromisoformat(entry["acquire_time"]),
                    target_time=date.fromisoformat(entry["target_time"]) if entry["target_time"] is not None else None,
                    source_refs=cls._source_refs(entry["message_ids"], messages, source.session_id),
                ) for entry in item["plans"])
                objects.append(MemoryObject(
                    object_id=str(uuid5(NAMESPACE_URL, position)), character_id=source.character_id,
                    object=item["object"], events=events, plans=plans, source_position_key=position,
                    created_at=now, forgetfulness=item["forgetfulness"],
                ))
            notes = []
            for item in data["selection_notes"]:
                cls._source_refs(item["message_ids"], messages, source.session_id)
                notes.append(ObjectSelectionNote(item["item"], item["decision"], item["reason"],
                                                 tuple(item["message_ids"])))
            return cls(tuple(objects), tuple(notes))
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise InvalidStateError(f"object模型输出不合法：{error}") from error

    @staticmethod
    def _source_refs(message_ids: list[str], messages: dict, session_id: str) -> tuple[EvidenceRef, ...]:
        if not isinstance(message_ids, list) or not message_ids:
            raise ValueError("message_ids必须是非空数组")
        refs = []
        for message_id in dict.fromkeys(message_ids):
            if message_id not in messages:
                raise ValueError("引用了本session以外的消息")
            turn_id, message = messages[message_id]
            refs.append(EvidenceRef(session_id, message_id, message.content, turn_id=turn_id))
        return tuple(refs)

    @staticmethod
    def model_output_schema(session_id: str | int, message_ids: tuple[str | int, ...]) -> dict:
        """模型输出契约；绑定本次session和可用消息ID，不包含存储管理字段。"""
        def closed_object(properties: dict) -> dict:
            return {"type": "object", "properties": properties,
                    "required": list(properties), "additionalProperties": False}

        source_id = {"type": ["string", "integer"]}
        if message_ids:
            source_id["enum"] = list(message_ids)
        refs = {"type": "array", "items": source_id, "minItems": 1}
        date = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
        optional_date = {"anyOf": [date, {"type": "null"}]}
        common = {
            "actor": {"type": "string", "enum": ["user", "assistant", "other"]},
            "statement": {"type": "string", "minLength": 1},
            "acquire_time": date,
        }
        event = closed_object({**common, "occurrence_time": optional_date, "message_ids": refs})
        plan = closed_object({**common, "target_time": optional_date, "message_ids": refs})
        note = closed_object({
            "item": {"type": "string"},
            "decision": {"type": "string", "enum": ["keep", "attach", "exclude"]},
            "reason": {"type": "string"},
            "message_ids": refs,
        })
        object_schema = closed_object({
            "object": {"type": "string", "minLength": 1},
            "events": {"type": "array", "items": event},
            "plans": {"type": "array", "items": plan},
            "forgetfulness": {"type": "integer", "enum": [0, 1]},
        })
        return closed_object({
            "session_id": {"type": "string" if isinstance(session_id, str) else "integer", "enum": [session_id]},
            "selection_notes": {"type": "array", "items": note},
            "objects": {"type": "array", "items": object_schema},
        })
