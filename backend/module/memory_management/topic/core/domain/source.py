"""Topic 整合时已加载的关联材料与输入分组。

TopicContext 绑定正式正文及版本，TopicObjectGroup 将新 object、关联 object 和已有 topic 组织为完整材料。
TopicSourceInput 核对材料与固定计划一致，并按共用时间线规则构造模型输入，不在此执行检索或查库。
"""
from __future__ import annotations
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from dataclasses import asdict, dataclass, replace
from datetime import date
from module.memory_management.object.core.domain.document import MemoryObject
from module.memory_management.topic.core.domain.document import TopicDocument
from module.memory_management.topic.core.domain.plan import TopicPlan


@dataclass(frozen=True, slots=True)
class TopicContext:
    """召回后从SQLite加载的正式正文及其固定版本。"""
    ref: MemoryRevisionRef
    document: TopicDocument

    def __post_init__(self) -> None:
        if self.ref.kind != MemoryKind.TOPIC:
            raise ValueError("topic上下文只能引用topic版本")


@dataclass(frozen=True, slots=True)
class TopicObjectGroup:
    """一个新object及其完整候选材料；跨组允许重复，同组不得重复。"""
    object: MemoryObject
    related_objects: tuple[MemoryObject, ...] = ()
    existing_topics: tuple[TopicContext, ...] = ()

    def __post_init__(self) -> None:
        if len({item.object_id for item in self.related_objects}) != len(self.related_objects):
            raise ValueError("同一object组内的历史object不能重复")
        if len({item.ref.memory_id for item in self.existing_topics}) != len(self.existing_topics):
            raise ValueError("同一object组内的topic不能重复")


@dataclass(frozen=True, slots=True)
class TopicSourceInput:
    """按新object组织完整材料，模型不需要根据ID查找另外的正文列表。"""
    plan: TopicPlan
    groups: tuple[TopicObjectGroup, ...]

    def __post_init__(self) -> None:
        if tuple(group.object.object_id for group in self.groups) != self.plan.object_ids:
            raise ValueError("topic输入分组必须按计划顺序覆盖本批新object")
        related_ids = tuple(dict.fromkeys(item.object_id for group in self.groups for item in group.related_objects))
        topic_refs = tuple(dict.fromkeys(item.ref for group in self.groups for item in group.existing_topics))
        if related_ids != self.plan.related_object_ids or topic_refs != self.plan.topic_refs:
            raise ValueError("topic分组材料与固定计划不一致")
        objects = [item for group in self.groups for item in (group.object, *group.related_objects)]
        if any(item.character_id != self.plan.character_id for item in objects):
            raise ValueError("topic材料混入其他角色的数据")

    @property
    def timeline_window_date(self) -> date:
        """用本批新对象的最新获知日作为重放时的时间线基准。"""
        return max(entry.acquire_time for group in self.groups
                   for entry in (*group.object.events, *group.object.plans))

    def to_model_payload(self) -> dict:
        window_date = self.timeline_window_date
        groups = []
        for group in self.groups:
            related = []
            for item in group.existing_topics:
                timeline = item.document.timeline_for(window_date)
                document, visible = timeline.document, timeline.visible_entries
                visible_ids = {entry.entry_id for entry in visible}
                payload = asdict(item)
                payload["document"] = asdict(replace(document, entries=visible,
                    current_state=replace(document.current_state, source_entry_ids=tuple(
                        entry_id for entry_id in document.current_state.source_entry_ids if entry_id in visible_ids))))
                payload["document"]["entries"] = list(payload["document"]["entries"])
                related.append(payload)
            groups.append({
                "OBJECT": asdict(group.object),
                "RELATED_TOPICS": related,
                "RELATED_OBJECTS": [asdict(item) for item in group.related_objects],
            })
        return {"PLAN": asdict(self.plan), "GROUPS": groups}
