"""用户编辑索引的串行后台领取与启动恢复。

复用通用 Worker 生命周期，领取编辑快照并交给 workflow，状态登记异常时保留当前项供下轮继续。
恢复在旧协程停止后执行，避免遗留 indexing 或并行执行相同领取代次。
"""
from module.shared.jobs.worker import Worker
from module.user_edit.memory_edit.core.scheduling.workflow import MemoryEditWorkflow
from module.user_edit.memory_edit.core.domain.ports import MemoryEditJobRepository
from module.user_edit.memory_edit.core.domain.job import MemoryEdit


class MemoryEditWorker(Worker):
    def __init__(self, repository: MemoryEditJobRepository, workflow: MemoryEditWorkflow, *,
                 poll_interval: float = 1.0) -> None:
        super().__init__(name="memory-edit-worker", poll_interval=poll_interval)
        self._repository, self._workflow = repository, workflow
        self._current: MemoryEdit | None = None

    async def recover(self) -> None:
        await self._repository.recover_memory_edit_indexes()
        self._current = None

    async def process_next(self) -> bool:
        # 若成功/失败登记本身遇到数据库异常，下轮继续处理本条，不能遗留indexing。
        if self._current is None:
            self._current = await self._repository.claim_next_memory_edit()
        if self._current is None:
            return False
        result = await self._workflow.execute(self._current)
        self._current = None
        return result
