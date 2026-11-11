"""generation.contracts: cohesive domain types; no external I/O."""
from __future__ import annotations

from module.chat.conversation.core.domain.contracts import AcceptedReply
from module.chat.reply.core.domain.materials import FrozenPrompt, GenerationInputBoundary
from module.memory_recall.agent.core.domain.audit import MemoryAudit
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GenerationStartTicket:
    """生成启动票据；应能从持久化Attempt重建，不能只存在内存里"""
    job_id: str #任务id
    attempt_id: str #尝试id
    turn_id: str #轮次id
    input_version: int #输入版本
    active_token: int #回复采用资格
    input_boundary: GenerationInputBoundary #T2-A输入边界


@dataclass(frozen=True, slots=True)
class GenerationFreezeTicket:
    """T2-B固定的生成清单，规定允许读取哪些输入，生成期间不可变更，后续将用次ticket调用模型"""
    job_id: str #提交材料所属任务
    attempt_id: str #提交材料所属尝试；必须仍是Turn的active_attempt_id
    turn_id: str #需要重新校验的当前轮
    session_id: str #需要校验的Turn所属Session
    input_version: int #本次组装依据的输入版本
    active_token: int #与T2-A取得的采用资格一致
    frozen_prompt: FrozenPrompt #完整提示词、hash、token计数及最终来源清单
    memory_audit: MemoryAudit | None = None #原始卡片与轨迹，随T2-B另存审查字段，不发给回复模型


@dataclass(frozen=True, slots=True)
class ReplyAcceptanceTicket:
    """回复采用票据"""
    attempt_id: str #尝试id
    input_version: int #输入版本
    active_token: int #回复采用资格
    prompt_hash: str #提示词哈希
    candidate_hash: str #候选哈希


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """调度器恢复扫描结果"""
    recovered_job_ids: tuple[str, ...] = () #恢复任务id
    obsolete_job_ids: tuple[str, ...] = () #过期任务id
    failed_job_ids: tuple[str, ...] = () #失败任务id
