"""记忆生产共用的session登记循环；具体材料准备由各链实现。"""
from __future__ import annotations
import logging
from module.memory_management.shared.jobs.core.domain.ports import MemoryJobRepository


logger = logging.getLogger(__name__)


class MemoryProductionService:
    _repository: MemoryJobRepository
    job_family: str

    async def _register_jobs_for_pending_sessions(self, *, limit: int, priority: int) -> tuple[str, ...]:
        session_ids = await self._repository.get_unregister_sealed_sessions(limit)
        job_ids = []
        for session_id in session_ids:
            try:
                job_ids.extend(await self._register_jobs_for_session(session_id, priority=priority))
            except NotImplementedError:
                raise
            except Exception as error:
                # 取消直接传播；异常日志不包含原文或数据库异常全文。
                logger.error("封存session的%s登记未完成：%s（%s）",
                             self.job_family, session_id, type(error).__name__)
        return tuple(job_ids)

    async def _register_jobs_for_session(self, session_id: str, *, priority: int) -> tuple[str, ...]:
        raise NotImplementedError("该记忆链的session登记尚未实现")
