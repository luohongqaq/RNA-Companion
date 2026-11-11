"""Topic 批量整合任务输入、阶段产物与发布检查。

TopicJobInput 固定角色和本批 object，TopicJob 保存来源、计划、生成结果、审核、待发布项及已发布引用。
负责核对任务类型、计划与批次一致性、审核绑定和索引文档，实际阶段执行由 processing 与 scheduling 完成。
"""
from __future__ import annotations
import hashlib
import json
from module.shared.common.errors.base import InvalidStateError
from module.memory_management.shared.core.domain.document import VectorDocument
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from dataclasses import dataclass
from module.memory_management.shared.jobs.core.domain.job import CreateMemoryJob, MemoryJob, MemoryJobInput, MemoryJobType
from module.memory_management.topic.core.domain.document import TopicDocument
from module.memory_management.topic.core.domain.generation import TopicApplication, TopicBatchResult
from module.memory_management.topic.core.domain.plan import TopicPlan
from module.memory_management.topic.core.domain.publication import TopicWriteItem
from module.memory_management.topic.core.domain.review import TopicReviewResult
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from module.memory_management.topic.core.domain.source import TopicSourceInput


@dataclass(frozen=True, slots=True, kw_only=True)
class TopicJobInput(MemoryJobInput):
    """本批新object身份；历史召回材料与各topic基础版本在生成计划中固定。"""
    target_kind: MemoryKind | None = MemoryKind.TOPIC
    object_ids: tuple[str, ...] = ()
    edited_document: TopicDocument | None = None

    def __post_init__(self) -> None:
        if (not self.character_id or any(not cid for cid in self.object_ids)
                or len(set(self.object_ids)) != len(self.object_ids)):
            raise ValueError("topic任务需要角色及不重复的object ID")

    @classmethod
    def create_command(cls, character_id: str, session_id: str, object_ids: tuple[str, ...], *,
                       priority: int = 10) -> CreateMemoryJob:
        """首次登记的一个批次；保留固定来源顺序，重放时沿用相同批次与顺序。"""
        if (not character_id or not session_id or not object_ids
                or any(not cid for cid in object_ids) or len(set(object_ids)) != len(object_ids)):
            raise ValueError('topic首次登记需要角色、session及不重复的非空object批次')
        ids = tuple(object_ids)
        digest = hashlib.sha256(json.dumps(ids, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
        return CreateMemoryJob(
            job_type=MemoryJobType.CONSOLIDATE_TOPIC,
            target_key=f'topic:{character_id}:consolidation',
            dedupe_key=f'topic:session:{character_id}:{session_id}:{digest}',
            priority=priority,
            input=cls(character_id=character_id, source_session_ids=(session_id,), object_ids=ids),
        )


@dataclass(slots=True)
class TopicJob(MemoryJob):
    """topic生成、更新和发布；由单入口串行决策。"""
    input: TopicJobInput
    generation_result: TopicBatchResult | None = None
    review: TopicReviewResult | None = None
    plan: TopicPlan | None = None
    generation_source: TopicSourceInput | None = None
    write_results: tuple[TopicWriteItem, ...] | None = None
    published_refs: tuple[MemoryRevisionRef, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.input, TopicJobInput) or self.job_type not in (
                MemoryJobType.CONSOLIDATE_TOPIC, MemoryJobType.CREATE_TOPIC, MemoryJobType.UPDATE_TOPIC, MemoryJobType.USER_EDIT):
            raise ValueError("TopicJob的任务类型或输入不匹配")
        if ((self.generation_result is not None and not isinstance(self.generation_result, TopicBatchResult))
                or (self.review is not None and not isinstance(self.review, TopicReviewResult))):
            raise ValueError("TopicJob只能保存topic候选和审核结果")
        if self.job_type == MemoryJobType.CONSOLIDATE_TOPIC and not self.input.object_ids:
            raise ValueError("topic整合任务必须指定本批object")
        if self.generation_source is not None and self.generation_source.plan != self.plan:
            raise ValueError("topic固定来源与保存计划不一致")
        if self.plan is not None:
            if self.plan.character_id != self.input.character_id or self.plan.object_ids != self.input.object_ids:
                raise ValueError("topic计划与登记批次不一致")
            if self.generation_result is not None:
                self.generation_result.validate_against_plan(self.plan)

    @property
    def publication_started(self) -> bool:
        return self.published_refs is not None or self.result is not None or self.applied_revision is not None

    def final_applications(self) -> tuple[TopicApplication, ...]:
        """沿用daily的审核绑定校验，返回整批通过或修订通过的申请。"""
        if self.generation_result is None or self.review is None:
            raise InvalidStateError("topic尚无已审核候选")
        if (self.reviewed_version != self.generation_result.result_version
                or self.reviewed_hash != self.generation_result.content_hash):
            raise InvalidStateError("topic审核未绑定当前候选")
        return self.review.accepted_applications(self.generation_result)

    def index_documents(self) -> tuple[VectorDocument, ...]:
        """复用待发布项的固定版本；索引重试不重新分配ID或版本。"""
        if self.write_results is None:
            raise InvalidStateError("topic尚无待发布正文")
        expected = tuple(TopicWriteItem.from_application(app) for app in self.final_applications())
        if self.write_results != expected:
            raise InvalidStateError("topic待发布结果与审核结果不一致")
        return tuple(VectorDocument(
            f"{item.ref.memory_id}:r{item.ref.revision}:{item.ref.content_hash}", item.ref,
            self.input.character_id, item.write.document, (), (), item.write.document.topic)
            for item in self.write_results)
