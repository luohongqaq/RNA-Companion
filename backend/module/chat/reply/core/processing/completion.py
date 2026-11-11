"""T2-B提交后的生成、候选持久化和T3采用编排。"""
from logger import stage
import logging
from collections.abc import Callable

from module.chat.reply.core.domain.ports import GenerationRepository, TokenCounter
from module.chat.reply.core.domain.fingerprint import ReplyFingerprint
from module.chat.reply.core.processing.service import GenerationReplyService
from module.chat.reply.core.domain.contracts import AcceptedReply, ReplyAcceptanceTicket
from module.chat.reply.core.domain.materials import PromptMessage
from module.chat.reply.core.domain.models import AttemptState
from module.shared.common.errors.base import InvalidStateError

logger = logging.getLogger(__name__)


class GenerationCompletionService:
    def __init__(self, repository: GenerationRepository, reply: GenerationReplyService,
                 token_counter: TokenCounter, *, notify_context: Callable[[], None] | None = None) -> None:
        self._repository = repository
        self._reply = reply
        self._token_counter = token_counter
        self._notify_context = notify_context

    @stage("generation.complete")
    async def complete(self, attempt_id: str) -> AcceptedReply | None:
        """只使用数据库已冻结的messages；已有候选时从采用阶段恢复。

        旧attempt由仓储拒绝，只有T3提交后返回正式回复。调用方负责单attempt的
        模型调用互斥；本入口不替代调度器，不发布前端事件。
        """
        attempt = await self._repository.get_attempt(attempt_id)
        if attempt is None or attempt.state not in (AttemptState.GENERATING, AttemptState.ACCEPTED):
            return None
        prompt = attempt.frozen_prompt
        if prompt is None:
            raise ValueError("生成阶段缺少已冻结提示词")
        candidate = attempt.candidate
        if candidate is None:
            # 检查一下数据库，如果还没生成就直接生成并返回
            if attempt.state == AttemptState.ACCEPTED:
                raise InvalidStateError("已采用attempt缺少持久化候选")
            result = await self._reply.generate(prompt, expected_prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages))
            candidate = result.candidate
            ticket = ReplyAcceptanceTicket(attempt_id, attempt.input_version, attempt.active_token,
                                            candidate.prompt_hash, candidate.candidate_hash)
            if not await self._repository.save_candidate(ticket, candidate):
                return None
        else:
            # 如果已经生成了，从数据库中直接读取
            ticket = ReplyAcceptanceTicket(attempt_id, attempt.input_version, attempt.active_token,
                                            candidate.prompt_hash, candidate.candidate_hash)
        # PromptBuilder约定本轮用户消息位于末尾；T2-B已逐条核对其原文和顺序。
        current_count = len(prompt.context_manifest.current_turn.message_ids)
        if current_count == 0:
            raise ValueError("冻结输入缺少本轮用户消息")
        # 聊天原文统一按本地 DeepSeek tokenizer 计数，包含实际保存的角色台词。
        # candidate.usage 是供应商整次请求用量，已随候选持久化，不能再叠加到原文计数。
        raw_turn = (*prompt.messages[-current_count:], PromptMessage('assistant', candidate.reply.response_zh))
        count = self._token_counter.count_messages(raw_turn)
        accepted = await self._repository.accept_reply(ticket, turn_token_count=count,
                                                   tokenizer_version=self._token_counter.tokenizer_version)
        if accepted is not None:
            logger.info("正式回复已提交", extra={"event": "reply_committed",
                        "conversation_id": accepted.conversation_id, "attempt_id": attempt_id})
        # accept_reply返回时T3已提交；通知仅加速扫描，失败仍由轮询兜底。
        if accepted is not None and self._notify_context is not None:
            try:
                self._notify_context()
            except Exception as error:
                logger.warning("上下文维护唤醒失败（%s）", type(error).__name__)
        return accepted
