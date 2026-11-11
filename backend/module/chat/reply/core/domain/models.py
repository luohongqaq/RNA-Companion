"""generation.models: cohesive domain types; no external I/O."""
from __future__ import annotations

from module.chat.reply.core.domain.materials import FrozenPrompt, GenerationInputBoundary, ReplyCandidate
from module.memory_recall.agent.core.domain.audit import MemoryAudit
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class GenerationJobStatus(StrEnum):
    """生成任务的下一步执行阶段，状态与阶段产物在同一事务提交"""
    NEED_PREPARE = "need_prepare" #创建或恢复Attempt，组装并冻结提示词
    NEED_GENERATE = "need_generate" #调用模型，保存完整候选
    NEED_ACCEPT = "need_accept" #尝试正式采用候选
    DONE = "done" #回复已采用
    OBSOLETE = "obsolete" #输入版本或采用资格失效
    CANCELLED = "cancelled" #主动取消
    FAILED = "failed" #永久失败


class AttemptState(StrEnum):
    """尝试阶段类别"""
    RETRIEVING = "retrieving" #已取得资格，提示词尚未冻结
    GENERATING = "generating" #提示词已冻结，等待生成或采用；不代表模型调用此刻正在执行，执行中状态来自调度器内存
    ACCEPTED = "accepted" #候选已正式采用
    SUPERSEDED = "superseded" #被新输入或新尝试替代
    FAILED = "failed" #本次尝试失败


class GenerationFailureKind(StrEnum):
    """生成失败类别"""
    NETWORK = "network"
    AUTH = "auth"
    QUOTA = "quota"
    MODEL = "model"
    CONTEXT_LIMIT = "context_limit"
    INVALID_OUTPUT = "invalid_output"
    STORAGE = "storage"
    OBSOLETE = "obsolete"


@dataclass(frozen=True, slots=True)
class GenerationFailure:
    """生成失败信息"""
    kind: GenerationFailureKind #失败类别
    code: str #具体错误码
    message: str #错误描述
    retryable: bool #是否可重试；暂时失败保留当前阶段，仅更新retry_count、available_at和错误信息
    occurred_at: datetime #发生时间


@dataclass(slots=True)
class GenerationJob:
    """某个Turn的某个输入版本需要生成回复"""
    job_id: str #任务id
    turn_id: str #所属轮
    expected_input_version: int #需要处理的输入版本
    status: GenerationJobStatus #下一步执行阶段
    priority: int #调度优先级
    available_at: datetime #最早执行时间
    dedupe_key: str #同一轮同一输入版本的去重键
    created_at: datetime #创建时间
    updated_at: datetime #更新时间
    current_attempt_id: str | None = None #当前尝试id，支持恢复
    attempt_count: int = 0 #已创建的尝试数量
    retry_count: int = 0 #当前阶段连续重试次数，成功推进阶段后重置
    last_error: GenerationFailure | None = None #最近一次错误
    finished_at: datetime | None = None #终止时间
    manual_retry_count: int = 0 #用户手动重试的累计次数；与阶段自动重试计数分开

    @classmethod
    def register_generate_reply(cls, job_id: str, turn_id: str, expected_input_version: int,
                  now: datetime) -> "GenerationJob":
        """登记回复生成任务：前台优先级、立即可执行、去重键绑定轮次与输入版本"""
        return cls(job_id=job_id, turn_id=turn_id,
                   expected_input_version=expected_input_version,
                   status=GenerationJobStatus.NEED_PREPARE, priority=0, available_at=now,
                   dedupe_key=f"generate_reply:{turn_id}:{expected_input_version}",
                   created_at=now, updated_at=now)


@dataclass(slots=True)
class GenerationAttempt:
    """一次具体生成尝试，保存输入证据和候选结果"""
    attempt_id: str #尝试id
    job_id: str #所属任务
    turn_id: str #所属轮
    attempt_seq: int #轮次内尝试序号；同一turn的新输入可能创建不同job
    input_version: int #本次使用的输入版本
    active_token: int #本次取得的回复采用资格
    state: AttemptState #尝试阶段及结局
    input_boundary: GenerationInputBoundary #T2-A固定的输入范围
    created_at: datetime #创建时间
    updated_at: datetime #更新时间
    frozen_prompt: FrozenPrompt | None = None #T2-B已冻结材料
    candidate: ReplyCandidate | None = None #已持久化的完整候选
    failure: GenerationFailure | None = None #失败信息
    finished_at: datetime | None = None #结束时间
    memory_audit: MemoryAudit | None = None #原始记忆卡片与检索证据，与frozen_prompt分开保存

    @classmethod
    def start(cls, attempt_id: str, job_id: str, turn_id: str, attempt_seq: int,
              input_version: int, active_token: int, input_boundary: GenerationInputBoundary,
              now: datetime) -> "GenerationAttempt":
        """任务领取时创建：retrieving状态，尚未冻结提示词"""
        return cls(attempt_id=attempt_id, job_id=job_id, turn_id=turn_id,
                   attempt_seq=attempt_seq, input_version=input_version,
                   active_token=active_token, state=AttemptState.RETRIEVING,
                   input_boundary=input_boundary, created_at=now, updated_at=now)

