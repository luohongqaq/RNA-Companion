"""会话封存用例：把当前窗口当成已满，走一次完整交接。"""
import asyncio

from logger import stage
from module.chat.conversation.core.domain.contracts import SealSessionResult
from module.chat.conversation.core.domain.ports import ContextHandoffPort, ConversationRepository
from module.shared.common.errors.base import InvalidInputError, InvalidStateError


class SessionSealService:
    """登记交接、置等待交接，并等待新窗口接替；摘要已就绪时立即返回。"""

    POLL_SECONDS = 0.5
    WAIT_SECONDS = 180.0

    def __init__(self, conversations: ConversationRepository, handoff: ContextHandoffPort,
                 *, wait_seconds: float = WAIT_SECONDS) -> None:
        self._conversations = conversations
        self._handoff = handoff
        self._wait_seconds = wait_seconds

    @stage("conversation.seal")
    async def seal(self, conversation_id: str, session_id: str) -> SealSessionResult:
        """封存指定会话；等待超时时接替会话为空，由调用方提示稍后刷新。"""
        for name, value in (("对话 ID", conversation_id), ("会话 ID", session_id)):
            if not isinstance(value, str) or not value.strip():
                raise InvalidInputError(f"{name}不能为空")
        # 获取需要封存的session的轮数
        through = await self._conversations.seal_target(conversation_id, session_id)
        await self._handoff.prepare_handoff(session_id, through)
        await self._conversations.mark_waiting_handoff(conversation_id, session_id)
        successor = await self._handoff.commit_handoff(session_id)
        if successor is not None:
            return SealSessionResult(conversation_id, session_id, successor.session_id)
        return SealSessionResult(
            conversation_id, session_id,
            await self._wait_for_successor(conversation_id, session_id),
        )

    async def _wait_for_successor(self, conversation_id: str, session_id: str) -> str | None:
        """交接由上下文流水线在后台完成；任务失败时回滚，避免对话卡在等待交接。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._wait_seconds
        while loop.time() < deadline:
            await asyncio.sleep(self.POLL_SECONDS)
            current = await self._conversations.get_current_session_id(conversation_id)
            if current is not None and current != session_id:
                return current
            if await self._handoff.handoff_status(session_id) == "failed":
                # 摘要重试也失败时不能把人锁在旧窗口里：丢掉摘要，只带冻结点之后的
                # 原文交接过去；兜底也交不出东西才回滚。
                degraded = await self._handoff.commit_degraded_handoff(session_id)
                if degraded is not None:
                    return degraded.session_id
                await self._conversations.release_handoff(conversation_id, session_id)
                raise InvalidStateError("摘要生成失败，这段对话已恢复为可继续聊天，稍后可以再试一次")
        return None
