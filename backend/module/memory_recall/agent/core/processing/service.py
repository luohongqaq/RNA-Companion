"""记忆召回对外业务入口：固定本次输入，衔接缓存、Agent 选择、总结与审计结果。"""
from __future__ import annotations
import asyncio
import copy
from collections.abc import Awaitable, Callable
from time import perf_counter
import hashlib
import json
from module.shared.context.types.working import WorkingContext
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from logger import stage
from module.memory_recall.agent.core.domain.errors import MemoryRecallTaskError
from module.memory_recall.agent.core.domain.ports import MemoryModelPort
from module.memory_recall.agent.core.domain.request import MemoryRecallTask
from module.memory_recall.agent.core.domain.resources import MemoryRecallResources
from module.memory_recall.agent.core.domain.result import MemoryContext, MemoryExcerpt, MemorySelection
from module.memory_recall.agent.core.processing.config.pipeline import MemoryRecallConfig
from module.memory_recall.agent.core.domain.policy import HARD_TIMEOUT_MULTIPLIER
from module.memory_recall.agent.core.processing.summary.service import MemorySummaryService
from module.memory_recall.agent.core.processing.selection.service import MemorySelectionService
from module.memory_recall.cache.core.processing.service import MemoryCacheService
from module.memory_recall.retrieval.core.domain.ports import MemoryReaderFactory
from module.memory_recall.retrieval.core.processing.session import MemoryReadSession


class MemoryRecallService:
    def __init__(self, model: MemoryModelPort, readers: MemoryReaderFactory,
                 resources: MemoryRecallResources, config: MemoryRecallConfig,
                 *, summary_gateway: MemoryModelPort | None = None,
                 memory_cache: MemoryCacheService | None = None, token_counter=None) -> None:
        self._model = model
        self._summary_service = MemorySummaryService(
            model if summary_gateway is None else summary_gateway, config.summary_model, resources.summary_prompt,
        )
        self._readers = readers
        self._resources = resources
        self._config = config
        self._token_counter = token_counter
        self._memory_cache = memory_cache

    @stage("memory.recall")
    async def prepare(self, task: MemoryRecallTask) -> MemoryContext:
        if task.character_card.character_id != task.character_id:
            raise MemoryRecallTaskError("角色卡与本次记忆作用域不一致")
        view = task.character_card.document.selection_view() #读取角色卡中相应规则
        history = self.project_chat_context(task.context) #组装历史对话记录
        current = "\n\n".join(message.content for message in task.context.current_input.user_messages) #组装本轮用户输入
        reader = self._readers.create(task.character_id)
        session = MemoryReadSession(reader)
        cache = None
        config = self._config
        async def select(allowance):
            nonlocal cache, session
            if self._memory_cache is not None and task.conversation_id is not None:
                cache = await self._memory_cache.prepare(task.character_id, task.conversation_id, reader)
                session = MemoryReadSession(reader, cache)
            return await MemorySelectionService.select(
                user_input=current, chat_context=history, instructions=self._resources.selection_instructions,
                store=session, client=self._model, model=config.selection_model,
                max_model_calls=config.max_model_calls, deadline_seconds=allowance,
                budget=config.budget, character_card=view, max_output_tokens=config.max_output_tokens,
                is_current=task.is_current,
                prefetch_enabled=config.prefetch_enabled, token_counter=self._token_counter,
            )
        pipeline = await self._run_pipeline(
            select=select, user_input=current, chat_context=history, summary_service=self._summary_service,
            total_seconds=config.total_seconds, selection_limit=config.selection_limit,
            target_seconds=config.target_seconds, max_output_tokens=config.max_output_tokens,
            character_context=view.selection_context(), is_current=task.is_current,
        )
        if cache is not None and (task.is_current is None or task.is_current()):
            await self._memory_cache.remember(task.character_id, task.conversation_id,
                                                cache, session.recent_retrievals())
        selection = pipeline["selection"]
        cards = tuple(copy.deepcopy(selection.get("selected", [])))
        refs, excerpts = [], []
        for card in cards:
            ref = MemoryRevisionRef(MemoryKind(card["kind"]), card["document_id"],
                                    card["revision"], card["source"]["content_hash"])
            if ref not in refs:
                refs.append(ref)
            for key, value in card["sections"].items():
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
                excerpts.append(MemoryExcerpt(ref, key, text))
        summary = pipeline["result"]
        text = summary["summary"]
        unresolved = list(selection.get("unresolved", []))
        if summary.get("error"):
            unresolved.append(summary["error"])
        return MemoryContext(
            attempt_id=task.attempt_id,
            status=("ready" if cards else "empty") if pipeline["completed"] else "incomplete",
            selection=MemorySelection(tuple(refs), tuple(excerpts), False), source_cards=cards,
            summary_text=text, summary_hash=hashlib.sha256(text.encode()).hexdigest() if text else "",
            summary_status=summary["status"], reason_code=selection.get("reason_code", "incomplete"),
            unresolved=tuple(unresolved), skill_hash=self._resources.skill_hash,
            summary_prompt_hash=self._resources.summary_prompt_hash, character_view_hash=view.source_hash,
            total_seconds=pipeline["total_seconds"],
            trajectory=tuple(copy.deepcopy(selection.get("trajectory", []))),
        )

    @staticmethod
    def project_chat_context(context: WorkingContext) -> list[dict]:
        """保留固定工作摘要、继承摘要和全部完整对话；context资料不冒充用户发言。"""
        result = []

        def turns(items):
            for turn in items:
                for message in turn.user_messages:
                    result.append({"role": "user", "content": message.content,
                                   "created_at": message.created_at.isoformat()})
                reply = turn.accepted_reply
                result.append({"role": "assistant", "content": reply.content,
                               "created_at": reply.created_at.isoformat()})

        inherited = context.inherited_context
        if inherited is not None:
            if inherited.snapshot is not None:
                result.append({"role": "context", "kind": "inherited_summary",
                               "content": inherited.snapshot.summary_text})
            turns(inherited.complete_turns)
        if context.working_snapshot is not None:
            result.append({"role": "context", "kind": "working_summary",
                           "content": context.working_snapshot.summary_text})
        turns(context.complete_turns)
        return result

    @staticmethod
    async def _run_pipeline(
        *, select: Callable[[float], Awaitable[dict]], user_input: str, chat_context: list,
        summary_service: MemorySummaryService,
        total_seconds: float = 12, selection_limit: float = 10, target_seconds: float = 11,
        max_output_tokens: int = 2048, character_context: dict | None = None,
        is_current: Callable[[], bool] | None = None,
    ) -> dict:
        """将记忆agent剩余的时间限制为总时间的fraction，调用记忆总结模型，并返回原始结果和总结结果"""
        if not 0 < target_seconds <= total_seconds or selection_limit <= 0:
            raise ValueError("无效的选择/总结截止时间")
        def ensure_current() -> None:
            if is_current is not None and not is_current():
                raise asyncio.CancelledError("当前输入已变化，丢弃旧attempt")
        started = perf_counter()
        hard_total_seconds = total_seconds * HARD_TIMEOUT_MULTIPLIER
        deadline = started + hard_total_seconds
        ensure_current()
        try:
            hard_allowance = min(selection_limit * HARD_TIMEOUT_MULTIPLIER,
                                 max(.001, deadline - perf_counter()))
            allowance = hard_allowance / HARD_TIMEOUT_MULTIPLIER
            selection = await asyncio.wait_for(select(allowance), timeout=hard_allowance)
        except TimeoutError:
            selection = {"reason_code": "incomplete", "selected": [], "unresolved": ["选择阶段达到硬保护时限"]}
        ensure_current()
        if not selection.get("selected"):
            result = {"status": "skipped", "summary": ""}
        else:
            result = await summary_service.summarize(
                selection=selection, user_input=user_input, chat_context=chat_context,
                deadline_at=deadline, target_deadline_at=started + target_seconds,
                max_output_tokens=max_output_tokens, character_context=character_context,
            )
        ensure_current()
        elapsed = perf_counter() - started
        completed = (selection.get("reason_code") != "incomplete" and elapsed <= hard_total_seconds
                     and (result["status"] == "summarized" if selection.get("selected")
                          else selection.get("reason_code") in ("context_sufficient", "no_relevant_memory", "empty_store")))
        return {"selection": selection, "result": result, "total_seconds": elapsed,
                "completed": completed, "over_target": elapsed > target_seconds,
                "over_deadline": elapsed >= hard_total_seconds,
                "over_soft_limit": elapsed > total_seconds,
                "hard_limit_seconds": hard_total_seconds}
