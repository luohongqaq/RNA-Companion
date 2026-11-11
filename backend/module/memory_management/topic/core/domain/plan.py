"""Topic 批量整合的固定执行计划。

TopicPlan 固定角色、本批 object、历史关联 object、已有 topic 基础版本，以及新建主题的不同会话门槛。
校验标识唯一性和版本类别，供来源组装、生成结果覆盖检查及任务恢复使用。
"""
from __future__ import annotations
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TopicPlan:
    """一次批处理的固定输入；与生成结果一起保存，不单独推进状态。"""
    character_id: str
    object_ids: tuple[str, ...]
    related_object_ids: tuple[str, ...] = ()
    topic_refs: tuple[MemoryRevisionRef, ...] = ()
    min_distinct_sessions: int = 3

    def __post_init__(self) -> None:
        ids = (*self.object_ids, *self.related_object_ids)
        if not self.character_id or not self.object_ids or any(not item for item in ids) or len(set(ids)) != len(ids):
            raise ValueError("topic计划需要角色和不重复的本批/历史object")
        if any(ref.kind != MemoryKind.TOPIC for ref in self.topic_refs):
            raise ValueError("topic计划只能固定topic基础版本")
        if len({ref.memory_id for ref in self.topic_refs}) != len(self.topic_refs):
            raise ValueError("同一topic不能固定多个基础版本")
        if type(self.min_distinct_sessions) is not int or self.min_distinct_sessions < 1:
            raise ValueError("topic建档的session门槛必须为正整数")
