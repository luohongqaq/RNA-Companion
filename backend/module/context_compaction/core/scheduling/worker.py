"""唯一上下文维护协程：启动恢复、串行调度、通知唤醒和停止。"""
from module.shared.jobs.worker import Worker
from module.context_compaction.core.scheduling.workflow import ContextWorkflow
from module.context_compaction.core.domain.ports import ContextMaintenanceRepository


class ContextWorker(Worker):
    def __init__(self, repository: ContextMaintenanceRepository, workflow: ContextWorkflow,
                 *, poll_interval: float = 1.0) -> None:
        super().__init__(name="context-worker", poll_interval=poll_interval)
        self._repository, self._workflow = repository, workflow

    async def recover(self) -> None:
        await self._repository.recover_unfinished_attempts()

    async def process_next(self) -> bool:
        job = await self._repository.get_next_executable_job()
        return job is not None and await self._workflow.execute(job.job_id)
