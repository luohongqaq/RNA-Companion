"""长期记忆任务编排：阶段推进、异常分类和有界退避。"""
from __future__ import annotations
import logging
from module.shared.jobs.config.retry import RetryPolicy
from datetime import datetime, timezone
from logger import stage
from module.memory_management.shared.contracts.errors import (
    MemoryIndexUnavailable,
    MemoryModelUnavailable,
    MemorySourceChangedError,
)
from module.memory_management.shared.jobs.core.domain.job import MemoryJob, MemoryJobStatus
from module.memory_management.shared.jobs.core.domain.ports import MemoryJobProcessor, MemoryJobRepository


logger = logging.getLogger(__name__)


class MemoryWorkflow:
    def __init__(self, repository: MemoryJobRepository, service: MemoryJobProcessor,
                 *, retry_policy: RetryPolicy | None = None) -> None:
        self._repository, self._service = repository, service
        self._retry_policy = RetryPolicy() if retry_policy is None else retry_policy

    @stage("memory.workflow", background=True)
    async def execute(self, job_id: str) -> bool:
        """推进一个任务；返回是否有进展，取消直接传播。"""
        job = await self._repository.get_job(job_id)
        if job is None or job.available_at > datetime.now(timezone.utc):
            return False
        if job.status in (MemoryJobStatus.WAITING_INDEX, MemoryJobStatus.DONE,
                          MemoryJobStatus.FAILED, MemoryJobStatus.OBSOLETE):
            return False
        if job.status in (MemoryJobStatus.GENERATING, MemoryJobStatus.REVIEWING, MemoryJobStatus.WRITING, MemoryJobStatus.INDEXING):
            # 唯一协程取到执行态时没有活跃调用，通常是上轮失败登记本身曾失败。
            return await self._handle_failure(job, TimeoutError())
        try:
            await self._service.process_job(job_id)
        except NotImplementedError:
            # 未实现的脚手架不能伪装成一次真实业务失败。
            raise
        except Exception as error:
            logger.warning("记忆任务阶段失败", exc_info=True, extra={"event": "memory_failure"})
            return await self._handle_failure(job, error)
        current = await self._repository.get_job(job_id)
        return current is not None and current != job

    async def _handle_failure(self, job: MemoryJob, error: Exception) -> bool:
        """重读阶段/代次→异常分类→retry_job或fail_job；不得回退已激活版本。"""
        current = await self._repository.get_job(job.job_id)
        if current is None:
            return False
        running_stages = {
            MemoryJobStatus.NEED_GENERATE: MemoryJobStatus.GENERATING,
            MemoryJobStatus.NEED_REVIEW: MemoryJobStatus.REVIEWING,
            MemoryJobStatus.NEED_WRITE: MemoryJobStatus.WRITING,
            MemoryJobStatus.NEED_INDEX: MemoryJobStatus.INDEXING,
        }
        same_stage = current.status == job.status and current.attempt_version == job.attempt_version
        own_started_stage = (current.status == running_stages.get(job.status)
            and current.attempt_version in (job.attempt_version, job.attempt_version + 1))
        if not (same_stage or own_started_stage) or current.retry_count != job.retry_count:
            return False
        source_changed = isinstance(error, MemorySourceChangedError)
        retryable = (error.retryable if isinstance(error, (MemoryModelUnavailable, MemoryIndexUnavailable)) else
            source_changed or isinstance(error, (TimeoutError, ConnectionError, OSError))
            or type(error).__name__ == "OperationalError")
        message = (str(error) if isinstance(error, (MemorySourceChangedError, MemoryModelUnavailable, MemoryIndexUnavailable))
                   else f"记忆任务阶段未完成（{type(error).__name__}）")
        published = current.publication_started
        if retryable and not (source_changed and published) and current.retry_count < self._retry_policy.max_retries:
            delay = self._retry_policy.delays[min(current.retry_count, len(self._retry_policy.delays)-1)]
            return await self._repository.retry_job(current.job_id, current.attempt_version,
                expected_status=current.status, available_at=datetime.now(timezone.utc)+delay,
                error=message, restart_generation=source_changed, expected_retry_count=current.retry_count)
        logger.error("记忆任务终止：%s（%s）", current.job_id, type(error).__name__)
        return await self._repository.fail_job(current.job_id, current.attempt_version, message,
            expected_status=current.status, expected_retry_count=current.retry_count)
