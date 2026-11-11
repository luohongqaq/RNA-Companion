"""Topic独立串行协程；与其他链并发，不与同链多执行者竞争。"""
from __future__ import annotations
from module.shared.jobs.worker import Worker
from module.memory_management.topic.core.domain.ports import TopicProductionRepository
from module.memory_management.topic.core.processing.service import TopicProductionService
from module.memory_management.topic.core.scheduling.workflow import TopicWorkflow


class TopicWorker(Worker):
    def __init__(self, repository: TopicProductionRepository, workflow: TopicWorkflow,
                 service: TopicProductionService, *, poll_interval: float = 1.0) -> None:
        super().__init__(name="topic-worker", poll_interval=poll_interval)
        self._repository, self._workflow, self._service = repository, workflow, service

    async def recover(self) -> None:
        await self._repository.recover_unfinished_attempts()

    async def process_next(self) -> bool:
        await self._service.register_topic_jobs()
        job = await self._repository.get_next_ready_job()
        return job is not None and await self._workflow.execute(job.job_id)
