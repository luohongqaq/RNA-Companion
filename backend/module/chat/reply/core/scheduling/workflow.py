"""完整回复流程编排：根据持久化Job阶段恢复，业务代码不持有数据库连接。"""
from logger import stage
from datetime import datetime, timezone
import logging
from module.shared.common.errors.base import InvalidStateError
from module.chat.reply.core.processing.completion import GenerationCompletionService
from module.chat.reply.core.domain.errors.reply import ReplyModelUnavailable, ReplyOutputError
from module.chat.reply.core.domain.job_ports import GenerationJobRepository
from module.chat.reply.core.processing.preparation import GenerationPreparationService
from module.chat.reply.core.domain.models import GenerationFailure, GenerationFailureKind, GenerationJobStatus
from module.shared.jobs.config.retry import RetryPolicy


class GenerationWorkflow:
    def __init__(self, repository: GenerationJobRepository, preparation: GenerationPreparationService,
                 completion: GenerationCompletionService, *, retry_policy: RetryPolicy | None = None) -> None:
        self._repository = repository
        self._preparation = preparation
        self._completion = completion
        self._retry_policy = retry_policy or RetryPolicy()

    @stage("generation.workflow", background=True)
    async def execute(self, job_id: str) -> bool:
        """执行一个任务；有进展返回True。取消直接传播，阶段故障持久化后退避。"""
        job = await self._repository.get_job(job_id)
        if job is None or job.status not in (
            GenerationJobStatus.NEED_PREPARE, GenerationJobStatus.NEED_GENERATE, GenerationJobStatus.NEED_ACCEPT,
        ) or job.available_at > datetime.now(timezone.utc):
            return False
        attempt_id = job.current_attempt_id
        try:
            if job.status == GenerationJobStatus.NEED_PREPARE:
                # need_prepare -> need_generate中间是生成票据、找到所有上下文和记忆，再组装成冻结提示
                # attempt 处于 retrieving状态，claim_job会检查attempt的存在，如果已有人持有该任务，会跳过返回none，一直卡着，需要先恢复
                ticket = await self._repository.resume_preparation(job_id)
                if ticket is None:
                    ticket = await self._repository.claim_job(job_id)
                if ticket is None:
                    return False
                attempt_id = ticket.attempt_id
                context = await self._preparation.load_context(ticket)
                if not await self._repository.check_attempt(attempt_id):
                    return False
                # 找到所有的上下文和记忆，然后冻结，即完成need_prepare
                frozen = await self._preparation.assemble(ticket, context)
                if not await self._repository.freeze_prompt(frozen):
                    return False
            if attempt_id is None:
                raise InvalidStateError("生成阶段缺少当前attempt")
            if not await self._repository.check_attempt(attempt_id):
                return False
            # need_generate -> need_accept -> done, 因此此步就是调用模型生成回复，need_accept是中间态，恢复用的，这里如果成功就直接done了
            await self._completion.complete(attempt_id)
            return True
        except Exception as exc:
            # CancelledError不属于Exception；关闭/取消不记成失败，重启继续已提交阶段。
            failure = self._failure(exc)
            logging.getLogger(__name__).warning("生成阶段失败，登记重试或终止", extra={
                "event": "generation_failure", "attempt_id": attempt_id,
            }, exc_info=True)
            await self._repository.record_failure(job_id, attempt_id, failure, self._retry_policy)
            return False

    @staticmethod
    def _failure(exc: Exception) -> GenerationFailure:
        retryable = not isinstance(exc, (InvalidStateError, ValueError, KeyError, TypeError, NotImplementedError))
        kind = GenerationFailureKind.STORAGE
        if isinstance(exc, ReplyModelUnavailable):
            retryable = exc.retryable
            kind = GenerationFailureKind.NETWORK
        elif isinstance(exc, ReplyOutputError):
            retryable, kind = True, GenerationFailureKind.INVALID_OUTPUT
        elif isinstance(exc, InvalidStateError):
            kind = GenerationFailureKind.CONTEXT_LIMIT if exc.code == 'prompt_budget_exceeded' else GenerationFailureKind.MODEL
        # 不把SDK异常全文、连接参数或模型原文写进面向前端的错误信息。
        return GenerationFailure(kind, getattr(exc, 'code', 'generation_stage_failed'),
            f"生成阶段未完成（{type(exc).__name__}）", retryable, datetime.now(timezone.utc))
