"""用户编辑索引阶段的失败分流与有界重试。

调用固定快照索引服务，根据可重试标记和公共退避策略登记重试或失败。
索引失败不回退用户正文，输入取消原样传播；状态更新通过仓储校验当前版本与执行代次。
"""
from datetime import datetime, timezone
import logging
from logger import stage

from module.shared.jobs.config.retry import RetryPolicy
from module.user_edit.memory_edit.core.processing.index_service import MemoryEditIndexService
from module.user_edit.memory_edit.core.domain.ports import MemoryEditJobRepository
from module.memory_management.shared.contracts.errors import MemoryIndexUnavailable
from module.user_edit.memory_edit.core.domain.job import MemoryEdit

logger = logging.getLogger(__name__)


class MemoryEditWorkflow:
    def __init__(self, repository: MemoryEditJobRepository, service: MemoryEditIndexService, *,
                 retry_policy: RetryPolicy | None = None) -> None:
        self._repository, self._service = repository, service
        self._retry_policy = retry_policy or RetryPolicy()

    @stage("memory.edit.workflow", background=True)
    async def execute(self, edit: MemoryEdit) -> bool:
        try:
            await self._service.index_memory_edit(edit)
        except Exception as error:
            logger.warning("用户编辑索引未完成：%s", edit.edit_id, exc_info=True)
            retryable = (error.retryable if isinstance(error, MemoryIndexUnavailable)
                         else isinstance(error, (TimeoutError, ConnectionError, OSError))
                         or type(error).__name__ == "OperationalError")
            message = (str(error) if isinstance(error, MemoryIndexUnavailable)
                       else f"编辑索引未完成（{type(error).__name__}）")
            if retryable and edit.retry_count < self._retry_policy.max_retries:
                delay = self._retry_policy.delays[min(edit.retry_count, len(self._retry_policy.delays) - 1)]
                return await self._repository.retry_memory_edit_index(edit.edit_id, edit.attempt_version,
                    available_at=datetime.now(timezone.utc) + delay, error=message)
            return await self._repository.fail_memory_edit_index(edit.edit_id, edit.attempt_version, error=message)
        return True
