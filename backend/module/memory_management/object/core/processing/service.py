"""Object提取、候选保存、混合索引与正式激活。"""
from __future__ import annotations
import json
from module.shared.common.errors.base import InvalidStateError, NotFoundError
from logger import stage
from module.memory_management.shared.indexing.core.domain.ports import MemoryIndexRepository
from module.memory_management.object.core.domain.job import ObjectJob
from module.memory_management.object.core.domain.job import ObjectJobInput
from module.memory_management.object.core.domain.generation import ObjectModelOutput
from module.memory_management.object.core.domain.ports import ObjectProductionRepository
from module.memory_management.object.core.domain.source import ObjectSourceInput
from module.memory_management.object.core.processing.config import ObjectModelConfig
from module.memory_management.object.core.processing.ports import ObjectModelPort
from module.memory_management.object.core.processing.prompts.generation import GENERATION_PROMPT
from module.memory_management.shared.contracts.errors import MemoryIndexUnavailable, MemoryModelUnavailable
from module.memory_management.shared.jobs.core.domain.job import MemoryJobStatus
from module.memory_management.shared.jobs.core.processing.service import MemoryProductionService


class ObjectProductionService(MemoryProductionService):
    job_family = "object"

    def __init__(self, repository: ObjectProductionRepository, *, model: ObjectModelPort | None = None,
                 model_config: ObjectModelConfig | None = None,
                 index_repository: MemoryIndexRepository | None = None) -> None:
        self._repository = repository
        self._model = model
        self._model_config = ObjectModelConfig() if model_config is None else model_config
        self._index_repository = index_repository

    async def register_object_jobs(self, *, limit: int = 32, priority: int = 10) -> tuple[str, ...]:
        """扫描封存身份并逐session登记；失败项留待下轮，不阻塞其他项。"""
        return await self._register_jobs_for_pending_sessions(limit=limit, priority=priority)

    async def _register_jobs_for_session(self, session_id: str, *, priority: int) -> tuple[str, ...]:
        character_id = await self._repository.read_session_character_id(session_id)
        command = ObjectJobInput.create_command(character_id, session_id, priority=priority)
        return (await self._repository.create_job(command),)

    async def process_job(self, job_id: str) -> None:
        job = await self._repository.get_job(job_id)
        if job is None:
            raise NotFoundError("object任务不存在")
        if not isinstance(job, ObjectJob):
            raise InvalidStateError("任务不属于object链")
        handlers = {
            MemoryJobStatus.NEED_GENERATE: self._generate,
            MemoryJobStatus.NEED_INDEX: self._index,
            MemoryJobStatus.NEED_ACTIVATE: self._activate,
        }
        handler = handlers.get(job.status)
        if handler is not None:
            await handler(job)

    @stage("memory.object.generate")
    async def _generate(self, job: ObjectJob) -> None:
        if self._model is None:
            raise NotImplementedError("object生成模型尚未接入")
        started = await self._repository.begin_generate(job.job_id)
        if started is None:
            return
        source = await self._repository.read_object_source(started)
        output = await self.generate_objects(source, feedback=job.last_error)
        await self._repository.save_extraction_result(started.job_id, started.attempt_version,
            output.objects, source=source, selection_notes=output.selection_notes)

    async def generate_objects(self, source: ObjectSourceInput, *, feedback: str | None = None) -> ObjectModelOutput:
        """事务外使用JSON模式生成，字段结构由提示词说明，返回后解析校验。"""
        if self._model is None:
            raise NotImplementedError("object生成模型尚未接入")
        config = self._model_config
        payload = source.to_model_payload()
        if feedback is not None:
            payload["PREVIOUS_ERRORS"] = feedback
        instructions, _, _ = GENERATION_PROMPT.partition("SESSION_MESSAGES:\n")
        schema = ObjectModelOutput.model_output_schema(source.session_id,
            tuple(message["message_id"] for message in payload["messages"]))
        instructions += "\n\n仅输出符合以下OUTPUT_SCHEMA的JSON对象：\n" + json.dumps(schema, ensure_ascii=False)
        data = await self._model.complete(instructions, payload, config)
        try:
            return ObjectModelOutput.from_model_data(data, source=source)
        except (InvalidStateError, TypeError, ValueError) as error:
            raise MemoryModelUnavailable(str(error), retryable=True) from error

    @stage("memory.object.index")
    async def _index(self, job: ObjectJob) -> None:
        if self._index_repository is None:
            raise NotImplementedError("object向量持久层尚未接入")
        started = await self._repository.begin_index(job.job_id)
        if started is None:
            return
        if not await self._index_repository.batch_object_index(started.objects):
            raise MemoryIndexUnavailable("object索引尚未确认完成")
        await self._repository.save_index(started.job_id, started.attempt_version)

    @stage("memory.object.activate")
    async def _activate(self, job: ObjectJob) -> None:
        await self._repository.activate_revision(job.job_id, job.attempt_version)
