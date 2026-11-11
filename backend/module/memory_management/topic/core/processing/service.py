"""Topic召回与批量结构化生成；候选入库、审核及发布仍待后续接入。"""
from __future__ import annotations
from module.shared.common.errors.base import InvalidStateError, NotFoundError
from logger import stage
from module.memory_management.shared.indexing.core.domain.ports import MemoryIndexRepository
from module.memory_management.object.core.domain.document import MemoryObject, ObjectState
from module.memory_management.shared.contracts.errors import MemoryIndexUnavailable, MemoryModelUnavailable
from module.memory_management.shared.jobs.core.domain.job import MemoryJobStatus
from module.memory_management.shared.contracts.review import ReviewVerdict
from module.memory_management.shared.jobs.core.processing.service import MemoryProductionService
from module.memory_management.topic.core.domain.generation import TopicBatchResult
from module.memory_management.topic.core.domain.plan import TopicPlan
from module.memory_management.topic.core.domain.job import TopicJob
from module.memory_management.topic.core.domain.job import TopicJobInput
from module.memory_management.topic.core.domain.generation import TopicModelOutput
from module.memory_management.topic.core.domain.ports import TopicProductionRepository
from module.memory_management.topic.core.domain.publication import TopicWriteItem
from module.memory_management.topic.core.domain.retrieval import (
    LinkedTopicRecall,
    RelatedObjectRecall,
    RelatedTopicRecall,
)
from module.memory_management.topic.core.domain.review import TopicApplicationReview, TopicReviewResult
from module.memory_management.topic.core.domain.source import TopicObjectGroup, TopicSourceInput
from module.memory_management.topic.core.processing.config import (
    ObjectRecallConfig,
    TopicModelConfig,
    TopicProductionConfig,
    TopicRecallConfig,
)
from module.memory_management.topic.core.processing.ports import (
    RelatedMemoryRerankPort,
    RelatedMemorySearchPort,
    TopicModelPort,
)
from module.memory_management.topic.core.processing.prompts.generation import GENERATION_PROMPT
from module.memory_management.topic.core.processing.related_retrieval import RelatedMemoryRetrieval


class TopicProductionService(MemoryProductionService):
    job_family = "topic"

    def __init__(self, repository: TopicProductionRepository, *,
                 model: TopicModelPort | None = None,
                 index_repository: MemoryIndexRepository | None = None,
                 model_config: TopicModelConfig | None = None,
                 config: TopicProductionConfig | None = None,
                 recall_repository: RelatedMemorySearchPort | None = None,
                 rerank_repository: RelatedMemoryRerankPort | None = None,
                 recall_config: ObjectRecallConfig | None = None,
                 topic_recall_config: TopicRecallConfig | None = None) -> None:
        self._repository = repository
        self._index_repository = index_repository
        self._model = model
        self._model_config = TopicModelConfig() if model_config is None else model_config
        self._config = TopicProductionConfig() if config is None else config
        self._retrieval = RelatedMemoryRetrieval(repository,
            recall_repository=recall_repository, rerank_repository=rerank_repository,
            recall_config=recall_config, topic_recall_config=topic_recall_config)

    async def recall_related_objects(self, objects: tuple[MemoryObject, ...]) -> tuple[RelatedObjectRecall, ...]:
        return await self._retrieval.recall_related_objects(objects)

    _select_reranked_hits = staticmethod(RelatedMemoryRetrieval._select_reranked_hits)

    async def recall_linked_topics(self, objects: tuple[MemoryObject, ...]) -> tuple[LinkedTopicRecall, ...]:
        return await self._retrieval.recall_linked_topics(objects)

    async def recall_related_topics(self, objects: tuple[MemoryObject, ...]) -> tuple[RelatedTopicRecall, ...]:
        return await self._retrieval.recall_related_topics(objects)

    async def register_topic_jobs(self, *, limit: int = 32, priority: int = 10) -> tuple[str, ...]:
        """首次登记：扫描object已DONE的封存session，逐session完整登记。
        单项失败不阻塞其他session，未标记项下轮继续。
        limit限制本轮扫描的session数，max_batch_objects限制每个任务的新object数。
        """
        return await self._register_jobs_for_pending_sessions(limit=limit, priority=priority)

    async def _register_jobs_for_session(self, session_id: str, *, priority: int) -> tuple[str, ...]:
        # 针对每一个session，只加载正式object，登记topic_job。
        # 独立读取角色，保证提取结果为空时仍能完成session的首次登记。
        character_id = await self._repository.read_session_character_id(session_id)
        objects = await self._repository.read_sealed_session_objects(session_id)
        batch_size = self._config.max_batch_objects
        commands = tuple(TopicJobInput.create_command(
            character_id, session_id,
            tuple(item.object_id for item in objects[start:start + batch_size]),
            priority=priority,
        ) for start in range(0, len(objects), batch_size))
        # 保留仓储给出的固定来源顺序；所有批次和完成标记由仓储原子提交。
        # 空结果也必须调用此方法，以免同一session被反复扫描。
        return await self._repository.register_topic_jobs_for_session(session_id, commands)

    async def process_job(self, job_id: str) -> None:
        job = await self._repository.get_job(job_id)
        if job is None:
            raise NotFoundError("topic任务不存在")
        if not isinstance(job, TopicJob):
            raise InvalidStateError("任务不属于topic链")
        handlers = {
            MemoryJobStatus.NEED_GENERATE: self._generate,
            MemoryJobStatus.NEED_REVIEW: self._review,
            MemoryJobStatus.NEED_WRITE: self._prepare_publication,
            MemoryJobStatus.NEED_INDEX: self._index,
            MemoryJobStatus.NEED_ACTIVATE: self._activate,
        }
        handler = handlers.get(job.status)
        if handler is not None:
            await handler(job)

    @stage("memory.topic.generate")
    async def _generate(self, job: TopicJob) -> None:
        """一次组织整批输入并生成候选；读取、检索和模型调用均在开始事务提交后执行。"""
        if self._model is None:
            raise NotImplementedError("topic生成模型尚未接入")
        started = await self._repository.begin_generate(job.job_id)
        if started is None:
            return

        # 1. 以仓储返回的最新任务为准，按登记顺序读取本批新object。
        character_id = started.input.character_id
        object_ids = started.input.object_ids
        # objects：MemoryObject 包含了正文
        objects = await self._repository.read_objects(object_ids, character_id=character_id)
        if tuple(item.object_id for item in objects) != object_ids:
            raise InvalidStateError("topic任务登记的新object缺失，不能缩小批次继续生成")
        if any(item.state != ObjectState.STAGED for item in objects):
            raise InvalidStateError("topic任务的新object已不处于STAGED，不能按未整合材料生成")

        # 2. 按新object组织两条召回链的完整正文；只在组内去重，不合并跨组正文。
        object_groups = {group.source_object_id: group for group in await self.recall_related_objects(objects)}
        topic_groups = {group.source_object_id: group for group in await self.recall_related_topics(objects)}
        own_ids = set(object_ids)
        groups = []
        for item in objects:
            object_group = object_groups.get(item.object_id)
            topic_group = topic_groups.get(item.object_id)
            related = {obj.object_id: obj for obj in (object_group.pending_objects if object_group else ())
                       if obj.object_id not in own_ids}
            topics = {topic.ref.memory_id: topic for topic in (topic_group.topics if topic_group else ())}
            # TopicObjectGroup包含了所有的正文
            groups.append(TopicObjectGroup(item, tuple(related.values()), tuple(topics.values())))

        # 3. 程序仅汇总ID/版本用于计划校验；提供给模型的正文仍保留在各自分组内。
        related_ids = tuple(dict.fromkeys(item.object_id for group in groups for item in group.related_objects))
        # topic 会持续更新，因此除了ID，还要固定“基于哪个版本生成更新”。因此这里用ref
        topic_refs = tuple(dict.fromkeys(item.ref for group in groups for item in group.existing_topics))
        plan = TopicPlan(character_id=character_id, object_ids=object_ids,
            related_object_ids=related_ids, topic_refs=topic_refs,
            min_distinct_sessions=self._config.min_distinct_sessions)

        source = TopicSourceInput(plan=plan, groups=tuple(groups))

        # 4. 整批只调用一次生成模型；直接使用object/topic正文，不重复读取session原文。
        generation_result = await self.generate_topics(source, feedback=job.last_error)
        # Plan、来源和候选通过同一保存接口提交，使用本次开始时登记的attempt_version。
        await self._repository.save_generation_result(started.job_id, started.attempt_version, generation_result, source=source)

    async def generate_topics(self, source: TopicSourceInput, *, feedback: str | None = None) -> TopicBatchResult:
        """按提示词规定的JSON结构整合；输出类负责解析、引用回填与候选校验。"""
        if self._model is None:
            raise NotImplementedError("topic生成模型尚未接入")
        config = self._model_config
        payload = source.to_model_payload()
        if feedback is not None:
            payload["PREVIOUS_ERRORS"] = feedback
        data = await self._model.complete(GENERATION_PROMPT, payload, config)
        try:
            return TopicModelOutput.from_model_data(data, source=source,
                                                    prompt=GENERATION_PROMPT).result
        except (InvalidStateError, TypeError, ValueError) as error:
            raise MemoryModelUnavailable(str(error), retryable=True) from error

    async def _review(self, job: TopicJob) -> None:
        """读取已保存的整批申请，一次审核并保存批量结论。"""
        started = await self._repository.begin_review(job.job_id)
        if started is None:
            return
        review = await self.review_topics(started.generation_result)
        await self._repository.save_review(started.job_id, started.attempt_version, review)

    async def review_topics(self, generation_result: TopicBatchResult) -> TopicReviewResult:
        """批量审核入口；当前版本审核直接通过，预留接入审核模型调用的位置。"""
        return TopicReviewResult(generation_result.result_version, generation_result.content_hash,
            tuple(TopicApplicationReview(app.application_id, ReviewVerdict.PASS)
                  for app in generation_result.applications))

    async def _prepare_publication(self, job: TopicJob) -> None:
        """读取整批已审核申请，事务外渲染，再原子保存待发布结果。"""
        started = await self._repository.begin_write(job.job_id)
        if started is None:
            return
        results = tuple(TopicWriteItem.from_application(app) for app in started.final_applications())
        await self._repository.save_write(started.job_id, started.attempt_version, results)

    @stage("memory.topic.index")
    async def _index(self, job: TopicJob) -> None:
        """沿用daily索引流程；逐篇复用分区接口，整批确认后提交进度。"""
        if job.write_results and self._index_repository is None:
            raise NotImplementedError("topic向量持久层尚未接入")
        started = await self._repository.begin_index(job.job_id)
        if started is None:
            return
        documents = started.index_documents()
        for item, document in zip(started.write_results, documents, strict=True):
            representations = item.write.index_representations(
                item.ref, started.input.character_id, started.input.recall_tier)
            if not await self._index_repository.batch_hybrid_index(document, representations):
                raise MemoryIndexUnavailable("topic索引尚未确认整批完成")
        await self._repository.save_index(started.job_id, started.attempt_version, documents)

    @stage("memory.topic.activate")
    async def _activate(self, job: TopicJob) -> None:
        """正式版及关联提交→事务外同步object元数据→核对版本并DONE。"""
        if job.final_applications() and self._index_repository is None:
            raise NotImplementedError("topic关联状态同步尚未接入")
        started = await self._repository.prepare_activation(job.job_id, job.attempt_version)
        if started is None:
            return
        # 读取SQLite当前正式object，不使用生成时仍处于STAGED的旧材料。
        applications = started.final_applications()
        expected = {cid: app.topic_id for app in applications for cid in app.object_ids}
        if expected:
            objects = await self._repository.read_objects(tuple(expected), character_id=started.input.character_id)
            if (tuple(item.object_id for item in objects) != tuple(expected)
                    or any(item.state != ObjectState.LINKED or item.linked_topic_id != expected[item.object_id]
                           for item in objects)):
                raise InvalidStateError("topic待同步的object关联与已发布申请不一致")
            # 仅覆盖两个分区的可变payload；失败保留NEED_ACTIVATE，整批重复同步可安全重试。
            if not await self._index_repository.update_object_metadata(objects):
                raise MemoryIndexUnavailable("topic关联的object元数据尚未确认同步完成")
        await self._repository.activate_revision(started.job_id, started.attempt_version)
