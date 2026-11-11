"""任务扫描、恢复与重试端口；执行器依赖生成事务能力，不持有具体存储。"""
from typing import Protocol
from module.chat.reply.core.domain.ports import GenerationRepository
from module.chat.reply.core.domain.contracts import GenerationStartTicket
from module.chat.reply.core.domain.models import GenerationFailure, GenerationJob
from module.shared.jobs.config.retry import RetryPolicy


class GenerationJobRepository(GenerationRepository, Protocol):
    async def cancel_job(self, job_id: str) -> GenerationJob:
        """原子取消当前有效的处理中任务；重复取消幂等，已完成/失效任务拒绝。"""
        ...

    async def list_conversation_jobs(self, conversation_id: str) -> tuple[GenerationJob, ...] | None:
        """当前输入的处理中/失败任务，包括退避中的任务；会话不存在返回 None。"""
        ...

    async def retry_failed_job(self, job_id: str) -> GenerationJob:
        """原子验证当前输入与会话，重新排队失败任务；重复点击不创建额外工作。"""
        ...

    async def list_ready_jobs(self, limit: int = 32) -> tuple[GenerationJob, ...]: ...

    async def get_job(self, job_id: str) -> GenerationJob | None: ...

    async def resume_preparation(self, job_id: str) -> GenerationStartTicket | None:
        """从持久化材料重建准备阶段票据。"""
        ...

    async def check_attempt(self, attempt_id: str) -> bool:
        """昂贵调用前复查任务资格。"""
        ...

    async def record_failure(self, job_id: str, attempt_id: str | None,
                             failure: GenerationFailure, policy: RetryPolicy) -> bool:
        """保留产物，安排退避或终止；拒绝迟到attempt的更新。"""
        ...
