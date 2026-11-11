"""上下文任务编排：推进持久化阶段，处理重试、恢复和失败登记。"""
from logger import stage
from datetime import datetime, timezone
import logging

from module.shared.common.errors.base import BusinessError
from module.context_compaction.core.domain.errors import ContextGenerationRejected, ContextModelUnavailable, ContextSourceChangedError
from module.context_compaction.core.processing.service import ContextCompactionService
from module.context_compaction.core.domain.ports import ContextMaintenanceRepository
from module.context_compaction.core.domain.job import ContextJob, ContextJobStatus
from module.shared.jobs.config.retry import RetryPolicy

logger = logging.getLogger(__name__)


class ContextWorkflow:
    def __init__(self, repository: ContextMaintenanceRepository, service: ContextCompactionService,
                 *, retry_policy: RetryPolicy | None = None) -> None:
        self._repository, self._service = repository, service
        self._retry_policy = RetryPolicy() if retry_policy is None else retry_policy

    @stage("context.workflow", background=True)
    async def execute(self, job_id: str) -> bool:
        """推进一个任务；返回是否有进展，取消直接传播。"""
        job = await self._repository.get_job(job_id)
        if job is None or job.available_at > datetime.now(timezone.utc):
            return False
        if job.status in (ContextJobStatus.GENERATING, ContextJobStatus.VALIDATING):
            # 只有一个执行者，此时没有活跃模型调用；通常是上轮异常登记曾失败。
            return await self._handle_failure(job,
                ContextModelUnavailable("上次阶段调用已结束，恢复遗留执行状态", retryable=True))
        if job.status not in (ContextJobStatus.NEED_GENERATE, ContextJobStatus.NEED_VALIDATE,
                              ContextJobStatus.NEED_PUBLISH):
            return False
        try:
            await self._service.process_job(job.job_id)
        except NotImplementedError:
            raise
        except Exception as error:
            logger.warning("上下文任务阶段失败", exc_info=True, extra={"event": "context_failure"})
            return await self._handle_failure(job, error)
        # worker 拿到 True 就立刻继续下一轮循环不睡觉
        current = await self._repository.get_job(job.job_id)
        return current is not None and current != job

    async def _handle_failure(self, job: ContextJob, error: Exception) -> bool:
        current = await self._repository.get_job(job.job_id)
        if current is None:
            return False
        running_stages = {
            ContextJobStatus.NEED_GENERATE: ContextJobStatus.GENERATING,
            ContextJobStatus.NEED_VALIDATE: ContextJobStatus.VALIDATING,
        }
        same_stage = current.status == job.status and current.attempt_version == job.attempt_version
        own_started_stage = (current.status == running_stages.get(job.status)
            and current.attempt_version == job.attempt_version + 1)
        if not (same_stage or own_started_stage) or current.retry_count != job.retry_count:
            return False
        job = current
        source_changed = isinstance(error, ContextSourceChangedError)
        if isinstance(error, ContextModelUnavailable):
            retryable = error.retryable
        else:
            retryable = (source_changed or isinstance(error, (TimeoutError, ConnectionError, OSError))
                or type(error).__name__ == "OperationalError")
        message = (str(error) if isinstance(error, (ContextModelUnavailable, ContextSourceChangedError))
                   else f"上下文任务阶段未完成（{type(error).__name__}）")
        if retryable and (isinstance(error, ContextGenerationRejected)
                          or job.retry_count < self._retry_policy.max_retries):
            delays = self._retry_policy.delays
            available_at = datetime.now(timezone.utc) + delays[min(job.retry_count, len(delays) - 1)]
            return await self._repository.retry_job(job.job_id, job.attempt_version,
                expected_status=job.status, available_at=available_at, error=message,
                restart_generation=source_changed, expected_retry_count=job.retry_count)
        if not isinstance(error, (BusinessError, ValueError, KeyError, TypeError, NotImplementedError)):
            logger.error("上下文任务终止：%s（%s）", job.job_id, type(error).__name__,
                exc_info=(type(error), error, error.__traceback__))
        return await self._repository.fail_job(job.job_id, job.attempt_version, message,
            expected_status=job.status, expected_retry_count=job.retry_count)
