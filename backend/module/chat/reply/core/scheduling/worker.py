"""单进程、单槽位的持久回复任务执行器；Event只负责加速唤醒。"""
from module.shared.jobs.worker import Worker
from module.chat.reply.core.scheduling.workflow import GenerationWorkflow
from module.chat.reply.core.domain.job_ports import GenerationJobRepository


class GenerationWorker(Worker):
    def __init__(self, repository: GenerationJobRepository, workflow: GenerationWorkflow,
                 *, poll_interval: float = 1.0) -> None:
        super().__init__(name="generation-worker", poll_interval=poll_interval)
        self._repository, self._workflow = repository, workflow

    async def recover(self) -> None:
        # 仓储的持久化阶段已支持续跑，无需启动时扫描或回退状态。
        pass

    async def process_next(self) -> bool:
        progressed = False
        for job in await self._repository.list_ready_jobs():
            progressed = await self._workflow.execute(job.job_id) or progressed
        return progressed
