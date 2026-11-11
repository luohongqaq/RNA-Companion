"""Object 正式事件对象及其值对象和状态。

MemoryObject 组合已发生进展 ObjectEvent 与未来打算 ObjectPlan，保存来源证据、冷热池和 topic 关联。
校验双时间信息、来源引用及对象内容，并提供统一索引文本；ObjectPlan 是记忆内容，不是任务执行计划。
"""
from __future__ import annotations
from module.shared.memory.types.references import EvidenceRef
from module.shared.memory.types.retention import RecallTier
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Literal


class ObjectState(StrEnum):
    """长期记忆候选状态"""
    STAGED = "staged"
    LINKED = "linked"
    DISCARDED = "discarded"


@dataclass(frozen=True, slots=True)
class ObjectEvent:
    """一条独立进展；得知日与实际发生日分别保存，未知发生日为None。"""
    actor: Literal["user", "assistant", "other"]
    statement: str
    acquire_time: date
    occurrence_time: date | None
    source_refs: tuple[EvidenceRef, ...]  # 由message_ids组装，不要求模型复制原文

    def __post_init__(self) -> None:
        if self.actor not in ("user", "assistant", "other"):
            raise ValueError("object进展主体不合法")
        if not isinstance(self.statement, str) or not self.statement.strip():
            raise ValueError("object进展陈述不能为空")
        if type(self.acquire_time) is not date or (self.occurrence_time is not None and type(self.occurrence_time) is not date):
            raise ValueError("object双时间线必须使用日期，未知发生日期用None")
        if not self.source_refs:
            raise ValueError("object进展必须有来源引用")


@dataclass(frozen=True, slots=True)
class ObjectPlan:
    """未来打算与目标日；目标日不代表事情已经发生。"""
    actor: Literal["user", "assistant", "other"]
    statement: str
    acquire_time: date
    target_time: date | None
    source_refs: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if self.actor not in ("user", "assistant", "other"):
            raise ValueError("object计划主体不合法")
        if not isinstance(self.statement, str) or not self.statement.strip():
            raise ValueError("object计划陈述不能为空")
        if type(self.acquire_time) is not date or (self.target_time is not None and type(self.target_time) is not date):
            raise ValueError("object计划必须使用日期，未知目标日期用None")
        if not self.source_refs:
            raise ValueError("object计划必须有来源引用")


@dataclass(frozen=True, slots=True)
class MemoryObject:
    """一件事情分别保存已发生进展与未来计划，不把目标日当发生日。"""
    object_id: str
    character_id: str
    object: str
    events: tuple[ObjectEvent, ...]
    source_position_key: str  # 来源位置生成的幂等键，不使用模型摘要hash
    created_at: datetime
    plans: tuple[ObjectPlan, ...] = ()
    forgetfulness: Literal[0, 1] = 1  # 0长期保留，1允许随时间淡出
    state: ObjectState = ObjectState.STAGED
    recall_tier: RecallTier = RecallTier.HOT
    linked_topic_id: str | None = None

    def index_text(self) -> str:
        """索引与内容查询共用完整陈述，不额外拼接日期或得知等包装。"""
        return "\n".join((self.object, *(item.statement for item in self.events),
                          *(item.statement for item in self.plans)))

    def __post_init__(self) -> None:
        if not isinstance(self.object, str) or not self.object.strip():
            raise ValueError("object名称不能为空")
        if not self.events and not self.plans:
            raise ValueError("object必须包含进展记录或计划")
        if any(not isinstance(event, ObjectEvent) for event in self.events):
            raise ValueError("object的events只能包含已发生进展")
        if any(not isinstance(plan, ObjectPlan) for plan in self.plans):
            raise ValueError("object的plans只能包含计划")
        if type(self.forgetfulness) is not int or self.forgetfulness not in (0, 1):
            raise ValueError("forgetfulness必须为整数0或1")
