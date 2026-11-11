"""Object独立串行协程；与其他链并发，不与同链多执行者竞争。"""
from __future__ import annotations
from module.shared.jobs.worker import Worker
from module.memory_management.object.core.domain.ports import ObjectProductionRepository
from module.memory_management.object.core.processing.service import ObjectProductionService
from module.memory_management.object.core.scheduling.workflow import ObjectWorkflow


class ObjectWorker(Worker):
    def __init__(self, repository: ObjectProductionRepository, workflow: ObjectWorkflow,
                 service: ObjectProductionService, *, poll_interval: float = 1.0) -> None:
        super().__init__(name="object-worker", poll_interval=poll_interval)
        self._repository, self._workflow, self._service = repository, workflow, service

    async def recover(self) -> None:
        await self._repository.recover_unfinished_attempts()

    async def process_next(self) -> bool:
        await self._service.register_object_jobs()
        job = await self._repository.get_next_ready_job()
        return job is not None and await self._workflow.execute(job.job_id)
