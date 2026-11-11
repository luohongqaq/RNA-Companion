"""事务外生成准备：加载上下文，编排记忆选择/总结与提示词构建。"""
from logger import stage

import asyncio

from module.chat.reply.core.processing.context_loader import ContextLoader
from module.shared.context.types.working import WorkingContext
from module.chat.reply.core.domain.contracts import GenerationStartTicket, GenerationFreezeTicket
from module.character.core.domain.models import CharacterCard
from module.memory_recall.shared.core.domain.errors import MemoryRecallUnavailable
from module.memory_recall.agent.core.processing.service import MemoryRecallService
from module.memory_recall.agent.core.domain.audit import MemoryAudit
from module.memory_recall.agent.core.domain.result import MemoryContext
from module.memory_recall.agent.core.domain.request import MemoryRecallTask
from module.memory_recall.agent.core.domain.summary import MemorySummary
from module.chat.reply.core.domain.ports import ReplyPromptResourcePort
from module.chat.reply.core.domain.errors.reply import ReplyModelUnavailable
from module.chat.scenes_recall.core.domain.errors import SceneRecallUnavailable
from module.chat.scenes_recall.core.domain.models import SceneQuery
from module.chat.scenes_recall.core.domain.ports import SceneRecallPort
from module.shared.context.types.messages import PromptMessage
from module.chat.reply.core.processing.prompt_builder import PromptBuilder
from collections.abc import Callable


class GenerationPreparationService:
    def __init__(self, context_loader: ContextLoader, memory_recall: MemoryRecallService | None = None,
                 *, prompt_resources: ReplyPromptResourcePort | None = None,
                 prompt_builder: PromptBuilder | None = None,
                 scene_recall: SceneRecallPort | None = None) -> None:
        self._context_loader = context_loader
        self._memory_recall = memory_recall
        self._prompt_resources = prompt_resources
        self._prompt_builder = prompt_builder
        self._scene_recall = scene_recall

    def disable_scene_recall(self) -> None:
        """场景运行时启动失败时由容器调用：整线停用场景召回，回复流程不再访问。"""
        self._scene_recall = None

    @stage("generation.memory.prepare")
    async def prepare_memory(
        self, ticket: GenerationStartTicket, context: WorkingContext, character_card: CharacterCard,
        *, is_current: Callable[[], bool] | None = None,
    ) -> MemoryContext:
        """T2事务外：复用固定上下文和同一角色卡，执行选择Skill及独立总结。

        返回正文、原始卡片和来源版本；不写库，不调用回复模型，不替代T2-B资格检查。
        """
        if self._memory_recall is None:
            raise MemoryRecallUnavailable("记忆召回尚未启用，请配置RNA_MEMORY_ENABLED及相关连接参数")
        return await self._memory_recall.prepare(MemoryRecallTask(
            ticket.attempt_id, ticket.input_boundary.character_id, context, character_card, is_current,
            conversation_id=ticket.input_boundary.conversation_id,
        ))

    @stage("generation.context.load")
    async def load_context(self, ticket: GenerationStartTicket) -> WorkingContext:
        """保留原票据资格，按固定引用加载正文；此阶段不推进数据库状态。

        输入:
        GenerationStartTicket: {job_id, attempt_id, turn_id, input_version, active_token,
                                input_boundary: GenerationInputBoundary}
        输出:
        WorkingContext: {working_snapshot: SnapshotContext|None,
                         complete_turns: tuple[CompletedTurnResult, ...],
                         current_input: CurrentTurnInput, inherited_context: InheritedContext|None}
        """
        boundary = ticket.input_boundary
        return await self._context_loader.load(
            working_snapshot_ref=boundary.working_snapshot,
            complete_turns=boundary.complete_turns,
            current_turn_ref=boundary.current_turn,
            inherited_context_ref=boundary.inherited_context,
        )

    @stage("generation.prompt.assemble")
    async def assemble(
        self, ticket: GenerationStartTicket, context: WorkingContext,
    ) -> GenerationFreezeTicket:
        """复用已加载上下文，读取角色资源、选择/总结记忆并构建冻结票据。

        角色资源、场景召回端口和PromptBuilder由宿主注入；场景与锚点在冻结前组装。
        不重复加载context，不检查生成资格，不写数据库或调用回复模型；
        返回材料仍需由T2-B成功提交后才能用于正式生成。

        输入:
        GenerationStartTicket: {job_id, attempt_id, turn_id, input_version, active_token,
                                input_boundary: GenerationInputBoundary}
        WorkingContext: {working_snapshot: SnapshotContext|None,
                         complete_turns: tuple[CompletedTurnResult, ...],
                         current_input: CurrentTurnInput, inherited_context: InheritedContext|None}
        输出:
        GenerationFreezeTicket: {job_id, attempt_id, turn_id, session_id, input_version,
                                 active_token, frozen_prompt: FrozenPrompt}
        """
        if self._prompt_resources is None or self._prompt_builder is None:
            raise NotImplementedError("角色资源读取和完整提示词构建尚未接入")
        # 根据角色ID拿到角色卡资源
        resources = await self._prompt_resources.load(ticket.input_boundary.character_id)
        scene_recall = self._scene_recall
        # 在qdrant中异步召回场景对话
        async def recall_scenes(recall: SceneRecallPort):
            inherited = context.inherited_context
            dialogue = (*PromptMessage.from_turns(inherited.complete_turns if inherited else ()),
                        *PromptMessage.from_turns(context.complete_turns),
                        *(PromptMessage("user", item.content) for item in context.current_input.user_messages))
            query = SceneQuery(ticket.input_boundary.character_id, dialogue[-6:])
            try:
                return await recall.search(query)
            except SceneRecallUnavailable as exc:
                raise ReplyModelUnavailable(str(exc)) from exc

        # 两个库只依赖已固定的上下文；任一分支失败或调用方取消时，回收另一个任务。
        memory_task = asyncio.create_task(self.prepare_memory(ticket, context, resources.character_card))
        pending = [memory_task]
        scene_task = None
        if scene_recall is not None and resources.scene_recall_enabled:
            scene_task = asyncio.create_task(recall_scenes(scene_recall))
            pending.append(scene_task)
        try:
            await asyncio.gather(*pending)
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        memory = memory_task.result()
        scenes = scene_task.result() if scene_task is not None else ()
        if memory.status == "incomplete":
            raise MemoryRecallUnavailable("记忆准备未完整交付；请处理失败或明确选择降级策略")
        prompt = self._prompt_builder.build(boundary=ticket.input_boundary, context=context,
                                             resources=resources, memory=MemorySummary.from_context(memory), scenes=scenes)
        return GenerationFreezeTicket(
            job_id=ticket.job_id, attempt_id=ticket.attempt_id, turn_id=ticket.turn_id,
            session_id=ticket.input_boundary.session_id, input_version=ticket.input_version,
            active_token=ticket.active_token, frozen_prompt=prompt, memory_audit=MemoryAudit.from_context(memory),
        )
