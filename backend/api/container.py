"""装配应用服务并管理后台任务与共享资源的生命周期。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from contextlib import AsyncExitStack
from pathlib import Path
import asyncio
from module.chat.conversation.core.processing.messages import MessageService
from module.chat.conversation.core.processing.events import ChatEventService
from module.chat.conversation.core.processing.conversations import ConversationService
from module.character.core.processing.service import CharacterService
from module.model_settings.core.processing.service import ModelSettingsService
from module.user_edit.memory_edit.core.processing.edit_service import MemoryEditService
from module.user_edit.memory_edit.core.processing.query_service import MemoryQueryService
from module.user_edit.memory_edit.core.processing.index_service import MemoryEditIndexService
from module.user_edit.memory_edit.repository.sqlite import SQLiteMemoryEditRepository
from module.user_edit.memory_edit.repository.qdrant import MemoryEditIndexRepository
from module.user_edit.memory_edit.core.scheduling.worker import MemoryEditWorker
from module.user_edit.memory_edit.core.scheduling.workflow import MemoryEditWorkflow
from module.model_settings.repository.dotenv import DotenvModelSettingsRepository
from module.chat.reply.core.processing.context_loader import ContextLoader
from module.context_compaction.core.processing.service import ContextCompactionService
from module.context_compaction.repository.llm import ContextSummaryModelRepository
from module.context_compaction.core.domain.policy import ContextCompactionPolicy, ContextModelConfig
from module.context_compaction.core.domain.lifecycle_policy import SessionLifecycleConfig
from module.chat.reply.core.domain.config.context_budget import ContextPromptBudget
from module.context_compaction.core.scheduling.worker import ContextWorker
from module.context_compaction.core.scheduling.workflow import ContextWorkflow
from module.context_compaction.repository.sqlite import SQLiteContextMaintenanceRepository
from module.chat.reply.core.processing.preparation import GenerationPreparationService
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.conversation.core.processing.seal import SessionSealService
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from module.chat.reply.repository.context_sqlite import SQLiteContextReadRepository
from storage.config.settings import Settings
from module.memory_recall.agent.core.processing.config.pipeline import MemoryRecallConfig
from module.memory_recall.agent.core.processing.service import MemoryRecallService
from module.memory_recall.agent.repository.llm import CompatibleMemoryModelGateway
from module.memory_recall.agent.repository.resources import MemoryRecallResourceLoader
from module.memory_recall.cache.core.processing.service import MemoryCacheService
from module.memory_recall.cache.repository.sqlite import SQLiteRecallCache
from module.memory_recall.retrieval.repository.reader_factory import RecallReaderFactory
from module.memory_recall.shared.core.domain.errors import MemoryRecallUnavailable
from storage.config.memory_runtime import MemoryStorageRuntime
from module.memory_management.daily.core.scheduling.worker import DailyWorker
from module.shared.common.time import BackendTime
from module.memory_management.retention.core.processing.service import MemoryRetentionService
from module.memory_management.retention.repository.sqlite import SQLiteMemoryRetentionRepository
from module.memory_management.shared.indexing.repository.qdrant import QdrantMemoryIndexRepository
from module.memory_management.daily.core.scheduling.workflow import DailyWorkflow
from module.memory_management.object.core.scheduling.worker import ObjectWorker
from module.memory_management.object.core.scheduling.workflow import ObjectWorkflow
from module.memory_management.topic.core.scheduling.worker import TopicWorker
from module.memory_management.topic.core.scheduling.workflow import TopicWorkflow
from module.memory_management.daily.core.processing.service import DailyProductionService
from module.memory_management.object.core.processing.service import ObjectProductionService
from module.memory_management.topic.core.processing.service import TopicProductionService
from module.memory_management.topic.core.processing.config import ObjectRecallConfig, TopicRecallConfig
from module.memory_management.topic.repository.sqlite import TopicProductionRepository
from module.memory_management.daily.core.processing.config import DailyModelConfig
from module.memory_management.object.core.processing.config import ObjectModelConfig
from module.memory_management.daily.repository.sqlite import DailyProductionRepository
from module.memory_management.daily.repository.qdrant import DailyIndexRepository
from module.memory_management.object.repository.sqlite import ObjectProductionRepository
from module.chat.reply.repository.prompt_resources import FileReplyPromptResourceReader
from module.character.repository.files import FileCharacterRepository
from module.character.repository.sqlite import SQLiteCharacterRepository
from module.chat.reply.core.processing.prompt_builder import PromptBuilder
from module.chat.reply.core.processing.service import GenerationReplyService
from module.chat.reply.core.domain.config.reply import ReplyModelConfig
from module.chat.scenes_recall.repository.runtime import SceneRuntime
from module.chat.reply.core.processing.completion import GenerationCompletionService
from module.chat.reply.core.processing.query import GenerationQueryService
from module.chat.reply.core.scheduling.worker import GenerationWorker
from module.chat.reply.core.scheduling.workflow import GenerationWorkflow
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from storage.llm.token_counter import TokenizerTokenCounter
from storage.llm.client_factory import ModelClientFactory, ModelConnection
from module.memory_management.daily.repository.llm import DailyModelRepository
from module.memory_management.object.repository.llm import ObjectModelRepository
from module.memory_management.topic.repository.llm import TopicModelRepository
from module.chat.reply.repository.llm import ReplyModelRepository
from logger import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ApplicationContainer:
    """应用包含的所有服务：数据库、消息服务、五条后台生产线、各 LLM 客户端"""
    # ------应用的最小骨架 --------------
    memory_storage: MemoryStorageRuntime
    settings: Settings
    message_service: MessageService #消息服务
    conversation_service: ConversationService
    chat_event_service: ChatEventService
    context_loader: ContextLoader #上下文加载器
    model_clients: ModelClientFactory = field(init=False)
    generation_preparation_service: GenerationPreparationService | None = None
    character_service: CharacterService | None = None
    model_settings_service: ModelSettingsService | None = None
    memory_query_service: MemoryQueryService | None = None
    memory_edit_service: MemoryEditService | None = None
    session_seal_service: SessionSealService | None = None
    context_lifecycle_policy: SessionLifecycleConfig = field(default_factory=SessionLifecycleConfig)
    retention_service: MemoryRetentionService | None = None
    memory_edit_index_service: MemoryEditIndexService | None = None
    memory_edit_worker: MemoryEditWorker | None = None

    # ------回复生成线 ------------------
    reply_settings: Settings | None = None # 回复线的开关
    scene_runtime: SceneRuntime | None = None # 场景召回运行时：独立的场景库qdrant，编码器复用记忆存储的
    reply_service: GenerationReplyService | None = None # 回复模型服务
    generation_repository: SQLiteGenerationRepository | None = None # 对话生成的存储层
    generation_query_service: GenerationQueryService | None = None #查询生成进度的服务
    generation_worker: GenerationWorker | None = None # 对话生成的worker
    token_counter: TokenizerTokenCounter | None = None # token计数器
    tokenizer_path: Path | None = None # tokenizer目录

    # ------上下文维护线 ------------------
    context_settings: Settings | None = None # 上下文维护的开关
    context_worker: ContextWorker | None = None # 上下文维护的worker，回复完成用notify_context启动

    # ------记忆基础设施 ------------------
    memory_production_settings: Settings | None = None # 记忆生产线的开关

    # --------记忆生产线--------------------------
    daily_production_service: DailyProductionService | None = None # 日记忆生产服务：把一天的对话浓缩成当日记忆
    daily_worker: DailyWorker | None = None # 日记忆生产的后台任务
    object_production_service: ObjectProductionService | None = None # 人物/物品记忆生产服务
    topic_production_service: TopicProductionService | None = None # 事件主题记忆生产服务
    object_worker: ObjectWorker | None = None # 人物/物品记忆生产的后台任务
    topic_worker: TopicWorker | None = None # 事件主题记忆生产的后台任务

    # -----------纯配置字段，不涉及依赖关系---------------
    generation_poll_seconds: float = 1.0 #轮询间隔
    generation_enabled: bool = False # 自动回复生成线的开关

    def __post_init__(self) -> None:
        settings = self.settings
        reply = self.reply_settings or settings
        context = self.context_settings or settings
        production = self.memory_production_settings or settings
        review_connection = ModelConnection(production.default_api_key, production.default_base_url)
        self.model_clients = ModelClientFactory({
            "reply": ModelConnection(reply.reply_api_key, reply.reply_base_url),
            "context_summary": ModelConnection(context.default_api_key, context.default_base_url),
            "recall_selection": ModelConnection(settings.memory_api_key, settings.memory_base_url),
            "recall_summary": ModelConnection(settings.memory_summary_api_key, settings.memory_summary_base_url),
            "daily_generation": ModelConnection(production.reply_api_key, production.reply_base_url),
            "daily_review": review_connection,
            "object_memory": review_connection,
            "topic_memory": review_connection,
        })

    @property
    def database(self) -> SQLiteDatabase:
        return self.memory_storage.database

    def notify_generation(self) -> None:
        # 叫醒自动回复的worker
        if self.generation_worker is not None:
            self.generation_worker.notify()

    def notify_context(self) -> None:
        # 叫醒上下文维护的worker
        if self.context_worker is not None:
            self.context_worker.notify()

    async def start(self) -> None:
        """开机：初始化存储、按开关逐条启动后台任务。任何一步失败，先关掉已启动的部分再抛错。"""
        try:
            await self.memory_storage.start()
            await SQLiteCharacterRepository(self.database).register_manifest_characters(Path(__file__).resolve().parents[1])
            settings = self.settings
            if settings.memory_enabled or settings.memory_production_enabled:
                index = (
                    self.memory_storage.get_index_repository() if settings.memory_embedding_path is not None
                    else QdrantMemoryIndexRepository(self.memory_storage.get_client(), None)
                )
                await index.initialize()
                retention = MemoryRetentionService(SQLiteMemoryRetentionRepository(self.database), index)
                self.retention_service = retention
                today = BackendTime.local(datetime.now(timezone.utc)).date()
                await retention.cool_expired_memories(today)
                await retention.cool_unlinked_objects(today)
            if self.token_counter is not None:
                if self.tokenizer_path is None:
                    raise ValueError("记忆召回或后台任务缺少tokenizer目录")
                await asyncio.to_thread(self.token_counter.load_directory, self.tokenizer_path)
            recall = None
            if settings.memory_enabled:
                gateway = CompatibleMemoryModelGateway(
                    self.model_clients.get("recall_selection"),
                    send_thinking_parameter=settings.memory_send_thinking_parameter,
                )
                separate_endpoint = settings.memory_summary_base_url != settings.memory_base_url
                summary_gateway = CompatibleMemoryModelGateway(
                    self.model_clients.get("recall_summary"),
                    send_thinking_parameter=settings.memory_send_thinking_parameter and not separate_endpoint,
                    always_thinking_effort="low" if separate_endpoint else None,
                )
                encoder = self.memory_storage.get_encoder() if settings.memory_embedding_path is not None else None
                recall = MemoryRecallService(
                    gateway,
                    RecallReaderFactory(self.memory_storage.get_client(), encoder, database=self.database, token_counter=self.token_counter),
                    MemoryRecallResourceLoader.load(),
                    MemoryRecallConfig(
                        selection_model=settings.memory_selection_model,
                        summary_model=settings.memory_summary_model or settings.memory_selection_model,
                        total_seconds=settings.memory_total_seconds,
                        target_seconds=settings.memory_target_seconds,
                        selection_limit=settings.memory_selection_seconds,
                        prefetch_enabled=settings.memory_prefetch_enabled,
                    ),
                    summary_gateway=summary_gateway,
                    memory_cache=MemoryCacheService(SQLiteRecallCache(self.database)), token_counter=self.token_counter,
                )
            self.generation_preparation_service = GenerationPreparationService(
                self.context_loader, recall,
                prompt_resources=FileReplyPromptResourceReader(self.database),
                prompt_builder=PromptBuilder(self.token_counter, anchor_position=settings.reply_anchor_position,
                                             example_format=settings.reply_example_format),
                scene_recall=self.scene_runtime,
            )
            if ((settings.memory_enabled or settings.memory_production_enabled)
                    and settings.memory_embedding_path is not None and self.memory_edit_service is not None):
                repository = SQLiteMemoryEditRepository(self.database)
                self.memory_edit_index_service = MemoryEditIndexService(repository,
                    index_repository=MemoryEditIndexRepository.from_shared(self.memory_storage.get_index_repository()))
                workflow = MemoryEditWorkflow(repository, self.memory_edit_index_service)
                self.memory_edit_worker = MemoryEditWorker(repository, workflow)
                self.memory_edit_service.set_index_notifier(self.memory_edit_worker.notify)
                await self.memory_edit_worker.start()
            # 启动场景召回；创建回复模型客户端，包装成回复服务
            if self.reply_settings is not None:
                settings = self.reply_settings
                if self.scene_runtime is not None:
                    try:
                        await self.scene_runtime.start()
                    except Exception as exc:
                        # 场景索引缺失属可选功能缺失：降级停用场景召回，其余功能照常启动。
                        # 场景召回按角色 manifest 的 scene_recall_enabled 生效，停用只影响检索不到示例对白。
                        log.warning("场景召回启动失败，已停用场景召回: %s", exc)
                        self.scene_runtime = None
                        self.generation_preparation_service.disable_scene_recall()
                self.reply_service = GenerationReplyService(
                    ReplyModelRepository(self.model_clients.get("reply")),
                    ReplyModelConfig(
                        settings.reply_model, settings.reply_temperature, settings.reply_max_output_tokens,
                        settings.reply_timeout_seconds, settings.reply_reasoning_effort,
                    ),
                )
            # 组装并启动上下文维护线：对话过长时后台把它压缩成摘要
            if self.context_settings is not None:
                settings = self.context_settings
                if self.token_counter is None or settings.default_api_key is None:
                    raise ValueError("上下文维护缺少tokenizer或摘要模型密钥")
                repository = SQLiteContextMaintenanceRepository(self.database, lifecycle_policy=self.context_lifecycle_policy)
                service = ContextCompactionService(prompt_budget=ContextPromptBudget(),
                    compaction_policy=ContextCompactionPolicy(), repository=repository,
                    token_counter=self.token_counter,
                    model=ContextSummaryModelRepository(self.model_clients.get("context_summary")),
                    model_config=ContextModelConfig(model_name=settings.default_model),
                    prompt_resources=FileReplyPromptResourceReader(self.database))
                workflow = ContextWorkflow(repository, service)
                self.context_worker = ContextWorker(repository, workflow, poll_interval=settings.context_poll_seconds)
                await self.context_worker.start()
            # 组装并启动记忆生产三条线：daily（日记忆）、object（人物/物品）、topic（事件主题）
            if self.memory_production_settings is not None:
                settings = self.memory_production_settings
                # 日记忆生产线：仓储+服务+后台任务
                repository = DailyProductionRepository(self.database)
                self.daily_production_service = DailyProductionService(repository,
                    model=DailyModelRepository(self.model_clients.get("daily_generation")),
                    review_model=DailyModelRepository(self.model_clients.get("daily_review")),
                    model_config=DailyModelConfig(), review_model_config=DailyModelConfig.for_review(),
                    prompt_resources=FileReplyPromptResourceReader(self.database),
                    index_repository=DailyIndexRepository.from_shared(self.memory_storage.get_index_repository()))
                workflow = DailyWorkflow(repository, self.daily_production_service)
                self.daily_worker = DailyWorker(repository, workflow, self.daily_production_service,
                    poll_interval=settings.memory_production_poll_seconds)
                await self.daily_worker.start()
                # 人物/物品记忆生产线
                object_repository = ObjectProductionRepository(self.database)
                self.object_production_service = ObjectProductionService(object_repository,
                    model=ObjectModelRepository(self.model_clients.get("object_memory")), model_config=ObjectModelConfig(),
                    index_repository=self.memory_storage.get_index_repository())
                object_workflow = ObjectWorkflow(object_repository, self.object_production_service)
                self.object_worker = ObjectWorker(object_repository, object_workflow, self.object_production_service,
                    poll_interval=settings.memory_production_poll_seconds)
                await self.object_worker.start()
                # 事件主题记忆生产线
                topic_repository = TopicProductionRepository(self.database)
                self.topic_production_service = TopicProductionService(topic_repository,
                    model=TopicModelRepository(self.model_clients.get("topic_memory")),
                    index_repository=self.memory_storage.get_index_repository(),
                    recall_repository=self.memory_storage.get_production_recall_repository(),
                    rerank_repository=self.memory_storage.get_rerank_repository(),
                    recall_config=ObjectRecallConfig(min_rerank_score=settings.memory_object_min_rerank_score),
                    topic_recall_config=TopicRecallConfig(min_rerank_score=settings.memory_topic_min_rerank_score))
                topic_workflow = TopicWorkflow(topic_repository, self.topic_production_service)
                self.topic_worker = TopicWorker(topic_repository, topic_workflow, self.topic_production_service,
                    poll_interval=settings.memory_production_poll_seconds)
                await self.topic_worker.start()
            # 组装并启动自动回复生成线：备料、生成、收尾的完整流程
            if self.generation_enabled:
                if self.token_counter is None or self.generation_repository is None or self.reply_service is None:
                    raise ValueError("自动回复缺少tokenizer、生成仓储或回复模型服务")
                completion = GenerationCompletionService(self.generation_repository,
                                                         self.reply_service, self.token_counter,
                                                         notify_context=self.notify_context)
                workflow = GenerationWorkflow(self.generation_repository, self.generation_preparation_service, completion)
                self.generation_worker = GenerationWorker(self.generation_repository, workflow,
                    poll_interval=self.generation_poll_seconds)
                await self.generation_worker.start()
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        # LIFO：先停generation/context/topic/object/daily worker，再关客户端和共享资源。
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(self._close_resources)
            cleanup.push_async_callback(self.model_clients.close)
            for worker in (self.memory_edit_worker, self.daily_worker, self.object_worker,
                           self.topic_worker, self.context_worker, self.generation_worker):
                if worker is not None:
                    cleanup.push_async_callback(worker.stop)

    async def _close_resources(self) -> None:
        # 关闭记忆存储和场景召回。
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(self.memory_storage.stop)
            if self.scene_runtime is not None:
                cleanup.push_async_callback(self.scene_runtime.stop)


def build_container(settings: Settings | None = None) -> ApplicationContainer:
    """组装容器：读配置、检查配置之间的依赖关系，再按开关把服务拼起来。只拼装不启动，启动靠start()。"""
    settings = settings or Settings()
    # 校验配置：开了某个功能，它依赖的密钥、模型路径必须齐全，缺了就在启动时报错，而不是跑到一半才炸
    if settings.reply_enabled and (settings.reply_api_key is None or not settings.reply_api_key.get_secret_value()):
        raise ValueError("启用回复需要RNA_REPLY_API_KEY")
    if settings.generation_enabled and (not settings.memory_enabled or not settings.reply_enabled
                                        or settings.reply_tokenizer_path is None):
        raise ValueError("启用自动回复需要RNA_MEMORY_ENABLED、RNA_REPLY_ENABLED及RNA_REPLY_TOKENIZER_PATH")
    context_enabled = settings.generation_enabled if settings.context_enabled is None else settings.context_enabled
    if context_enabled and (settings.default_api_key is None or not settings.default_api_key.get_secret_value()
                            or settings.reply_tokenizer_path is None):
        raise ValueError("启用上下文维护需要DEFAULT_API_KEY及RNA_REPLY_TOKENIZER_PATH")
    if settings.memory_production_enabled:
        if (settings.reply_api_key is None or not settings.reply_api_key.get_secret_value()
                or settings.default_api_key is None or not settings.default_api_key.get_secret_value()):
            raise ValueError("记忆生产需要GLM的RNA_REPLY_API_KEY和DeepSeek的DEFAULT_API_KEY")
        if settings.memory_qdrant_path is None or settings.memory_embedding_path is None:
            raise ValueError("记忆生产需要RNA_MEMORY_QDRANT_PATH及本地BGE-M3路径")
    if settings.memory_enabled:
        if settings.reply_tokenizer_path is None:
            raise ValueError("记忆召回需要RNA_REPLY_TOKENIZER_PATH")
        if settings.memory_api_key is None or not settings.memory_api_key.get_secret_value():
            raise MemoryRecallUnavailable("请配置RNA_MEMORY_API_KEY")
        if settings.memory_qdrant_path is None:
            raise MemoryRecallUnavailable("请配置RNA_MEMORY_QDRANT_PATH")
        if not settings.memory_qdrant_path.is_dir():
            # 首次使用的空库：目录随启动创建，记忆集合在使用中建立
            settings.memory_qdrant_path.mkdir(parents=True, exist_ok=True)
            log.info("已创建记忆库目录: %s", settings.memory_qdrant_path)
    # 仓储共用存储运行时持有的数据库，启动时统一初始化。
    memory_storage = MemoryStorageRuntime(settings)
    database = memory_storage.database
    # 窗口额度与交接水位由环境变量控制，用户可按需调大或调小。
    context_lifecycle = SessionLifecycleConfig(
        session_target_tokens=settings.context_session_target_tokens,
        session_prepare_ratio=settings.context_session_prepare_ratio,
        session_hard_ratio=settings.context_session_hard_ratio,
    )
    chat_repository = SQLiteChatRepository(database)
    # 手工封存复用上下文交接流水线；上下文维护未启用时该能力关闭。
    session_seal_service = (
        SessionSealService(chat_repository, SQLiteContextMaintenanceRepository(database, lifecycle_policy=context_lifecycle))
        if context_enabled else None
    )
    context_loader = ContextLoader(SQLiteContextReadRepository(database))
    encoder_provider = memory_storage.get_encoder if settings.memory_enabled or settings.memory_production_enabled else None
    scene_runtime = SceneRuntime(settings, encoder_provider) if settings.reply_enabled and settings.reply_scene_enabled else None
    generation_repository = SQLiteGenerationRepository(database, lifecycle_policy=context_lifecycle)
    counter = TokenizerTokenCounter() if settings.generation_enabled or context_enabled or settings.memory_enabled else None
    # 把所有服务装进容器
    container = ApplicationContainer(
        memory_storage=memory_storage,
        settings=settings,
        message_service=MessageService(chat_repository),
        chat_event_service=ChatEventService(chat_repository),
        conversation_service=ConversationService(chat_repository),
        session_seal_service=session_seal_service,
        context_lifecycle_policy=context_lifecycle,
        character_service=CharacterService(SQLiteCharacterRepository(database), FileCharacterRepository(database)),
        model_settings_service=ModelSettingsService(DotenvModelSettingsRepository(database, active_settings=settings)),
        memory_query_service=MemoryQueryService(SQLiteMemoryEditRepository(database)),
        memory_edit_service=MemoryEditService(SQLiteMemoryEditRepository(database)),
        context_loader=context_loader,
        memory_production_settings=settings if settings.memory_production_enabled else None,
        reply_settings=settings if settings.reply_enabled else None,
        scene_runtime=scene_runtime,
        generation_repository=generation_repository,

        token_counter=counter, tokenizer_path=settings.reply_tokenizer_path,
        generation_poll_seconds=settings.generation_poll_seconds,
        generation_enabled=settings.generation_enabled,
        context_settings=settings if context_enabled else None,
    )
    # 回填两个依赖容器自身的服务：MessageService需要"叫醒生成worker"的回调，得等容器建好才能拿到
    container.message_service = MessageService(chat_repository, notify_generation=container.notify_generation)
    container.generation_query_service = GenerationQueryService(
        generation_repository, container.message_service, notify_generation=container.notify_generation,
    )
    return container
