"""Daily维护协程：启动恢复、串行调度、通知唤醒和停止。"""
from __future__ import annotations
from module.shared.jobs.worker import Worker
from module.memory_management.daily.core.domain.ports import DailyProductionRepository
from module.memory_management.daily.core.processing.service import DailyProductionService
from module.memory_management.daily.core.scheduling.workflow import DailyWorkflow


class DailyWorker(Worker):
    def __init__(self, repository: DailyProductionRepository, workflow: DailyWorkflow,
                 service: DailyProductionService, *, poll_interval: float = 1.0) -> None:
        super().__init__(name="daily-worker", poll_interval=poll_interval)
        self._repository, self._workflow, self._service = repository, workflow, service

    async def recover(self) -> None:
        await self._repository.recover_unfinished_attempts()

    async def process_next(self) -> bool:
        await self._service.register_daily_jobs()
        job = await self._repository.get_next_ready_job()
        return job is not None and await self._workflow.execute(job.job_id)
