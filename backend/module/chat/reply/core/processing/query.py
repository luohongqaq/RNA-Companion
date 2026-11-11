"""api层查询任务阶段、重试任务的service层。"""
from logger import stage
from collections.abc import Callable
import logging

from module.shared.common.errors.base import InvalidInputError, NotFoundError
from module.chat.reply.core.domain.job_ports import GenerationJobRepository
from module.chat.conversation.core.processing.messages import MessageService
from module.chat.reply.core.domain.models import GenerationJob, GenerationJobStatus
from module.chat.reply.core.domain.query import GenerationJobResult


class GenerationQueryService:
    def __init__(self, repository: GenerationJobRepository, chat_service: MessageService,
                 *, notify_generation: Callable[[], None] | None = None) -> None:
        self._repository = repository
        self._chat_service = chat_service
        self._notify_generation = notify_generation

    async def get(self, job_id: str) -> GenerationJobResult:
        job = await self._repository.get_job(job_id)
        if job is None:
            raise NotFoundError("生成任务不存在")
        return await self._result(job)

    async def list_for_conversation(self, conversation_id: str) -> tuple[GenerationJobResult, ...]:
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise InvalidInputError("会话 ID 不能为空")
        jobs = await self._repository.list_conversation_jobs(conversation_id)
        if jobs is None:
            raise NotFoundError("会话不存在")
        return tuple([await self._result(job) for job in jobs])

    @stage("generation.manual_retry")
    async def retry(self, job_id: str) -> GenerationJobResult:
        """重试生成任务"""
        job = await self._repository.retry_failed_job(job_id)
        if self._notify_generation is not None:
            try:
                self._notify_generation()
            except Exception as exc:
                logging.getLogger(__name__).warning("重试通知未送达（%s）；持久任务保留", type(exc).__name__)
        return await self._result(job)

    @stage("generation.cancel")
    async def cancel(self, job_id: str) -> GenerationJobResult:
        """逻辑取消；不依赖中断模型 HTTP 请求来保证正式回复不被采用。"""
        return await self._result(await self._repository.cancel_job(job_id))

    async def _result(self, job: GenerationJob) -> GenerationJobResult:
        job_id = job.job_id

        reply = await self._chat_service.reply_message(job_id) if job.status == GenerationJobStatus.DONE else None
        return GenerationJobResult(
            job_id=job.job_id, turn_id=job.turn_id, input_version=job.expected_input_version,
            status=job.status, retry_count=job.retry_count, available_at=job.available_at,
            error=job.last_error, reply=reply, manual_retry_count=job.manual_retry_count,
        )
