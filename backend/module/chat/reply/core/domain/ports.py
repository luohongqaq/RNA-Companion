"""生成业务的持久化、提示词资源读取与token计数端口；实现在storage层。"""

from collections.abc import Sequence
from typing import Protocol

from module.chat.reply.core.domain.config.reply import ReplyModelConfig
from module.chat.reply.core.domain.contracts import AcceptedReply, GenerationFreezeTicket, GenerationStartTicket, ReplyAcceptanceTicket
from module.chat.reply.core.domain.materials import ReplyModelResponse, PromptMessage, ReplyCandidate, ReplyPromptResources
from module.chat.reply.core.domain.models import GenerationAttempt


class ReplyPromptResourcePort(Protocol):
    async def load(self, character_id: str) -> ReplyPromptResources:
        """按角色配置加载角色卡、系统提示词、输出要求和版本；资源缺失明确失败。"""
        ...


class TokenCounter(Protocol):
    @property
    def tokenizer_version(self) -> str:
        """实际计数使用的tokenizer及消息序列化计数规则版本。"""
        ...

    def count_messages(self, messages: Sequence[PromptMessage]) -> int:
        """计算最终消息序列的输入token用量，包含适配协议的消息开销。"""
        ...


class GenerationRepository(Protocol):
    async def get_attempt(self, attempt_id: str) -> GenerationAttempt | None:
        """读取已提交的冻结提示词及候选，供阶段恢复使用。"""
        ...

    async def save_candidate(self, ticket: ReplyAcceptanceTicket, candidate: ReplyCandidate) -> bool:
        """原子保存完整候选并推进need_accept；同一候选重试幂等，失去资格返回False。"""
        ...

    async def accept_reply(
        self, ticket: ReplyAcceptanceTicket, *, turn_token_count: int, tokenizer_version: str,
    ) -> AcceptedReply | None:
        """T3：采用持久候选、封口轮次并累计原文token；重复采用返回原消息。

        turn_token_count须在锁外统计本轮用户原文与正式回复，不使用模型完整prompt用量。
        资格失效返回None；存储故障及材料损坏直接报错。
        """
        ...

    async def claim_job(self, job_id: str) -> GenerationStartTicket | None:
        """T2-A：原子领取一个待生成任务。

        仅当任务仍具备资格时领取：job 可领取且已到期、无在途attempt、
        turn 仍 current 且 input_version 与 job 期望一致、conversation active、session open。
        成功则创建 retrieving attempt、递增 active_token、固定输入边界并返回票据；
        资格永久失效时在同一事务将 job 置为终态（obsolete/cancelled）并返回 None；
        仅是被抢先领取、退避中或不存在时返回 None 且不写库。存储异常照常传播。

        输入:
        job_id
        输出:
        GenerationStartTicket: {job_id, attempt_id, turn_id, input_version, active_token,
                                input_boundary: GenerationInputBoundary}|None
            """
    async def freeze_prompt(self, ticket: GenerationFreezeTicket) -> bool:
        """T2-B：原子校验资格并保存完整材料。

        资格失效返回False，不覆盖已有冻结材料；提交成功才返回True。
        材料中的current_turn必须与Ticket的turn_id、input_version一致，
        消息ID、Session以及Job/Attempt关联关系均在事务内校验。
        存储异常向调用方传播，不能作为资格失效吞掉。
        此操作不读取模型服务，不重新选取最新摘要或记忆版本。

        输入:
        GenerationFreezeTicket: {job_id, attempt_id, turn_id, session_id, input_version,
                                 active_token, frozen_prompt: FrozenPrompt}
        输出:
        bool
        """
        ...


class ReplyModelPort(Protocol):
    """调用模型并返回标准化的回复响应。"""

    async def complete(self, messages: tuple[PromptMessage, ...], config: ReplyModelConfig) -> ReplyModelResponse: ...
