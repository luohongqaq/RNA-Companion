"""压缩任务、阶段恢复、快照发布和交接事务的接口。"""
from __future__ import annotations
from module.context_compaction.core.domain.policy import ContextModelConfig
from typing import Protocol
from module.chat.conversation.core.domain.models import Session
from module.shared.context.types.references import InheritedContextRef, SnapshotRef
from module.context_compaction.core.domain.job import ContextJob, ContextJobStatus, CreateContextJobCommand
from module.context_compaction.core.domain.plan import CompactionPlan, TurnTokenInfo
from module.context_compaction.core.domain.snapshots import ContextSnapshot
from module.context_compaction.core.domain.candidate import SummaryCandidate, SummaryValidation
from datetime import datetime
from module.context_compaction.core.domain.source import SummaryModelInput


class ContextMaintenanceRepository(Protocol):
    """持久化阶段产物；过时代次/阶段返回 False 或 None，不覆盖新状态。

    publish保存READY快照并将任务设为READY；交接成功才将任务设为DONE。
    采用与旧窗口封存、后继创建和当前窗口指针更新使用同一事务；
    commit_handoff是独立事务交接入口，具体适配器另提供事务内触发方法。
    """

    async def create_job(self, command: CreateContextJobCommand) -> str: ...
    async def get_job(self, job_id: str) -> ContextJob | None: ...
    async def get_next_executable_job(self) -> ContextJob | None:
        """供唯一维护协程读取下一个到期任务；只读，不领取或修改状态。

        返回可推进态（非READY）的队首任务；READY等待交接，由交接事务处理。
        """
        ...
    async def read_current_snapshot(self, session_id: str) -> ContextSnapshot | None:
        """读取指定 session 的 VALID 完整快照，无有效摘要返回 None。

        session不存在抛NotFoundError；摘要缺少来源清单时由mapper明确报错。
        """
        ...
    async def read_inherited_context(self, session_id: str) -> InheritedContextRef | None:
        """读取session保存的固定继承引用；首窗口返回None，缺失session或后继引用明确报错。"""
        ...
    async def read_model_input(self, plan: CompactionPlan) -> SummaryModelInput:
        """同一读事务读取计划范围正文、固定继承材料及其实际来源清单，不写任务。"""
        ...
    async def read_turn_token_infos(
        self, session_id: str, through_turn_seq: int,
    ) -> tuple[TurnTokenInfo, ...]:
        """读取 1～固定上界的连续完整轮统计；上界为 0 返回空元组。

        session 不存在抛 NotFoundError；范围缺失、未完成或计数不可用抛 InvalidStateError。
        """
        ...
    async def begin_generate(self, job_id: str) -> ContextJob | None:
        """只登记执行代次并进入GENERATING；非到期队首返回None。"""
        ...
    async def begin_validate(self, job_id: str) -> ContextJob | None:
        """保留生成恢复点，登记独立执行代次并进入VALIDATING。"""
        ...
    async def save_candidate(self, job_id: str, attempt_version: int, candidate: SummaryCandidate,
                             *, source: SummaryModelInput) -> bool:
        """原子保存本次计划、来源清单、候选并进入NEED_VALIDATE。"""
        ...
    async def save_validation(
        self, job_id: str, attempt_version: int, validation: SummaryValidation,
    ) -> bool:
        """保存当前候选的程序校验；通过后待发布，失败重试或结束。"""
        ...
    async def publish_ready_snapshot(
        self, job_id: str, attempt_version: int,
    ) -> SnapshotRef | None:
        """发布交接快照并进入任务READY，尚未交接时不标记DONE。"""
        ...
    async def commit_handoff(self, session_id: str) -> Session | None:
        """完整轮边界提交交接；READY→DONE与session路由切换同事务，重复提交返回原后继。"""
        ...
    async def fail_job(self, job_id: str, attempt_version: int, error: str, *,
                       expected_status: ContextJobStatus, expected_retry_count: int | None = None) -> bool:
        """仅结束当前执行阶段和代次，不覆盖READY及终态。"""
        ...
    async def retry_job(self, job_id: str, attempt_version: int, *, expected_status: ContextJobStatus,
                        available_at: datetime, error: str, restart_generation: bool = False,
                        expected_retry_count: int | None = None) -> bool:
        """执行态退回对应待执行态；仅来源失效时清除产物重新生成。"""
        ...
    async def recover_unfinished_attempts(self) -> int:
        """启动协程前恢复GENERATING/VALIDATING，递增执行代次并保留已提交产物。"""
        ...


class ContextSummaryModelPort(Protocol):
    """摘要模型调用端口，返回已解析的 JSON 对象。"""

    async def complete(self, *, prompt: str, payload: dict, model: str, config: ContextModelConfig) -> dict: ...
