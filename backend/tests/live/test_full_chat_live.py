r"""真实模型两场景：项目 .env + 只读现有记忆 + 临时聊天 SQLite。

需要本机私有运行数据（backend/.env 密钥、本地记忆 Qdrant 库与模型目录）；
无这些数据时不设置 RNA_LIVE_TEST 即整体跳过，开源用户无需关心本文件。

PowerShell：
    $env:RNA_LIVE_TEST = '1'
    .\.venv\Scripts\python.exe -m pytest tests/live/test_full_chat_live.py -v -s --durations=0

GLM 回复，DeepSeek 选择/总结；无模型或数据库替身。Qdrant 原库只读生成一致性
临时快照，SDK 只打开快照（它没有只读打开模式，会写锁/初始化 SQLite）。
聊天、缓存、任务、候选、审计均写入每个场景独立的 tmp_path，不打开正式聊天库。
未配置 GLM tokenizer 时，回退使用已随仓库分发的 DeepSeek tokenizer，明确记录为近似计数。
不手写计时；pytest --durations 报告 setup/call/teardown，call 含阶段断言。
两个场景独立，不构成连续两轮。逐步调用真实阶段，未启动自动 worker/lifespan。
"""

import asyncio
from contextlib import AsyncExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import httpx
from openai import AsyncOpenAI
import pytest
import pytest_asyncio
from dotenv import dotenv_values
from pydantic import SecretStr
from sqlalchemy import select

from api.app import create_app
from api.dependencies import get_generation_query_service, get_message_service
from module.chat.conversation.core.processing.messages import MessageService
from module.chat.reply.core.processing.context_loader import ContextLoader
from module.chat.reply.core.processing.completion import GenerationCompletionService
from module.chat.reply.core.domain.config.reply import ReplyModelConfig
from module.chat.reply.core.processing.preparation import GenerationPreparationService
from module.chat.reply.core.processing.prompt_builder import PromptBuilder
from module.chat.reply.core.domain.fingerprint import ReplyFingerprint
from module.chat.reply.core.processing.service import GenerationReplyService
from module.chat.reply.core.domain.contracts import ReplyAcceptanceTicket
from module.chat.reply.core.domain.materials import PromptMessage
from module.chat.scenes_recall.core.domain.models import SceneQuery
from module.chat.reply.core.processing.query import GenerationQueryService
from module.memory_recall.agent.core.processing.config.pipeline import MemoryRecallConfig
from module.memory_recall.cache.core.processing.service import MemoryCacheService
from module.memory_recall.agent.core.processing.service import MemoryRecallService
from module.memory_recall.agent.repository.resources import MemoryRecallResourceLoader
from module.memory_recall.retrieval.repository.reader_factory import RecallReaderFactory
from module.memory_recall.agent.repository.llm import CompatibleMemoryModelGateway
from storage.config.memory_runtime import MemoryStorageRuntime
from module.chat.reply.repository.prompt_resources import FileReplyPromptResourceReader
from module.chat.scenes_recall.repository.runtime import SceneRuntime
from storage.config.settings import Settings
from storage.llm.token_counter import TokenizerTokenCounter
from storage.qdrant.memory_schema import COLLECTION
from module.chat.scenes_recall.repository.index import QdrantSceneIndex
from module.character.repository.files import FileCharacterRepository
from module.character.repository.sqlite import SQLiteCharacterRepository
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from module.chat.reply.repository.context_sqlite import SQLiteContextReadRepository
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from storage.sqlite.models import GenerationAttemptRow, GenerationJobRow, MessageRow, SessionRow, TurnRow
from module.memory_recall.cache.repository.sqlite import SQLiteRecallCache
from module.chat.reply.repository.llm import ReplyModelRepository


ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(os.getenv("RNA_LIVE_TEST") != "1", reason="真实调用需显式设置 RNA_LIVE_TEST=1"),
]


class DeepSeekApproximateCounter:
    """真实 DeepSeek tokenizer，显式标注其对 GLM 仅作近似，不伪装成 GLM 计数。"""

    def __init__(self, actual_counter):
        self.actual_counter = actual_counter

    @property
    def tokenizer_version(self):
        return "approximate-for-glm:deepseek:" + self.actual_counter.tokenizer_version

    def count_text(self, text):
        return self.actual_counter.count_text(text)

    def count_messages(self, messages):
        return self.actual_counter.count_messages(messages)


def load_project_settings():
    """只读取本项目配置，分别保留 GLM 和 DeepSeek 凭据与模型名。"""
    env_file = ROOT / ".env"
    assert env_file.is_file(), "本项目 backend/.env 不存在"
    settings = Settings(_env_file=env_file)
    values = dotenv_values(env_file)
    default_key = os.getenv("DEFAULT_API_KEY") or values.get("DEFAULT_API_KEY")
    memory_key = settings.memory_api_key or (SecretStr(default_key) if default_key else None)
    selection_model = settings.memory_selection_model or os.getenv("DEFAULT_MODEL") or values.get("DEFAULT_MODEL")
    assert memory_key and memory_key.get_secret_value(), "项目 .env 缺少 DeepSeek API key"
    assert selection_model, "项目 .env 缺少 DeepSeek 模型名"
    assert settings.reply_api_key and settings.reply_api_key.get_secret_value(), "项目 .env 缺少 GLM API key"

    # 空字符串会变成 Path('.')；仅在未配置时使用用户明确允许的 DS 近似计数。
    tokenizer_path = settings.reply_tokenizer_path
    approximate = not tokenizer_path or str(tokenizer_path) == "."
    if approximate:
        tokenizer_path = ROOT / "var/seed_memory/deepseek_tokenizer/deepseek_v4_tokenizer"
    elif not tokenizer_path.is_absolute():
        tokenizer_path = ROOT / tokenizer_path
    qdrant_path = settings.memory_qdrant_path
    assert qdrant_path is not None, "请配置 RNA_MEMORY_QDRANT_PATH"
    if not qdrant_path.is_absolute():
        qdrant_path = ROOT / qdrant_path
    encoder_path = settings.memory_embedding_path
    assert encoder_path is not None, "请配置 RNA_MEMORY_EMBEDDING_PATH"
    if not encoder_path.is_absolute():
        encoder_path = ROOT / encoder_path
    assert tokenizer_path.is_dir(), "本地 tokenizer 目录不存在"
    assert encoder_path.is_dir(), "本地 embedding 目录不存在"
    return settings.model_copy(update={
        "memory_api_key": memory_key,
        "memory_base_url": settings.memory_base_url or os.getenv("DEFAULT_BASE_URL") or values.get("DEFAULT_BASE_URL"),
        "memory_selection_model": selection_model,
        "memory_summary_model": settings.memory_summary_model or selection_model,
        "memory_qdrant_path": qdrant_path.resolve(),
        "memory_embedding_path": encoder_path.resolve(),
        "reply_tokenizer_path": tokenizer_path.resolve(),
    }), approximate


def snapshot_memory_readonly(source, destination, collection=COLLECTION):
    """只读原记忆：SQLite mode=ro + backup，不在源目录启动 Qdrant 客户端。"""
    meta_path = source / "meta.json"
    source_db = source / "collection" / collection / "storage.sqlite"
    assert meta_path.is_file() and source_db.is_file(), "现有本地 Qdrant 记忆库不存在"
    meta_bytes = meta_path.read_bytes()
    meta = json.loads(meta_bytes)
    assert collection in meta["collections"], f"原库没有{collection} collection"
    target_collection = destination / "collection" / collection
    target_collection.mkdir(parents=True)
    (destination / "meta.json").write_text(json.dumps({
        "collections": {collection: meta["collections"][collection]}, "aliases": {},
    }), encoding="utf-8")
    with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as reader:
        reader.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(target_collection / "storage.sqlite")) as writer:
            reader.backup(writer)
    return {
        "meta": hashlib.sha256(meta_bytes).hexdigest(),
        "storage": hashlib.sha256(source_db.read_bytes()).hexdigest(),
    }


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_resources(tmp_path_factory):
    # 重模型只加载一次；pytest 将准备耗时显示为 setup。
    settings, approximate = load_project_settings()
    original_memory = settings.memory_qdrant_path
    snapshot = tmp_path_factory.mktemp("live_memory") / "qdrant"
    before = snapshot_memory_readonly(original_memory, snapshot)
    scene_source = settings.reply_scene_qdrant_path.resolve()
    scene_snapshot = tmp_path_factory.mktemp("live_scenes") / "qdrant"
    scene_before = None
    if settings.reply_scene_enabled:
        scene_before = snapshot_memory_readonly(scene_source, scene_snapshot, "character_scenes")
        (scene_snapshot / "ready.json").write_bytes((scene_source / "ready.json").read_bytes())
        settings = settings.model_copy(update={"reply_scene_qdrant_path": scene_snapshot})
    runtime_settings = settings.model_copy(update={"memory_qdrant_path": snapshot,
                                                  "memory_enabled": True, "memory_production_enabled": False})
    print(f"\n只读记忆来源：{original_memory}\n真实检索快照：{snapshot}", flush=True)
    print(f"回复：{settings.reply_model}；记忆选择：{settings.memory_selection_model}；总结：{settings.memory_summary_model}", flush=True)
    if approximate:
        print("计数：按要求使用 DeepSeek tokenizer 近似 GLM，不代表 GLM 实际计费 token。", flush=True)
    try:
        async with AsyncExitStack() as cleanup:
            storage = MemoryStorageRuntime(runtime_settings, database_path=snapshot.parent / "recall.sqlite3")
            cleanup.push_async_callback(storage.stop)
            await storage.start()
            recall_client = AsyncOpenAI(api_key=settings.memory_api_key.get_secret_value(),
                                       base_url=settings.memory_base_url or None, max_retries=0)
            cleanup.push_async_callback(recall_client.close)
            gateway = CompatibleMemoryModelGateway(
                recall_client, send_thinking_parameter=settings.memory_send_thinking_parameter)
            memory_runtime = SimpleNamespace(complete=gateway.complete, qdrant=storage.get_client(),
                                             encoder=storage.get_encoder() if settings.memory_embedding_path else None)
            scene_runtime = SceneRuntime(settings, storage.get_encoder) if settings.reply_scene_enabled else None
            reply_client = AsyncOpenAI(api_key=settings.reply_api_key.get_secret_value(),
                                       base_url=settings.reply_base_url, max_retries=0)
            reply_service = GenerationReplyService(ReplyModelRepository(reply_client), ReplyModelConfig(
                settings.reply_model, settings.reply_temperature, settings.reply_max_output_tokens,
                settings.reply_timeout_seconds, settings.reply_reasoning_effort))
            cleanup.push_async_callback(reply_client.close)
            if scene_runtime is not None:
                cleanup.push_async_callback(scene_runtime.stop)
                await scene_runtime.start()
            if settings.reply_scene_enabled:
                # 独立的真实检索正例：用库中已知情境验证BGE/Qdrant确实能命中。
                # 不降低两个聊天场景的阈值、不强塞参考，也不增加外部模型调用。
                scene_character = next(
                    (item for item in FileCharacterRepository.discover_sources(ROOT)
                     if (ROOT / item.card_path / "scenes" / "sessions_train_by_expr").is_dir()), None)
                if scene_character is None:
                    raise ValueError("没有具备场景数据的已登记角色，无法运行场景检索正例")
                scene_source = (settings.reply_scene_source_path
                                or ROOT / scene_character.card_path / "scenes" / "sessions_train_by_expr")
                rows = QdrantSceneIndex.load_scenes(scene_source, scene_character.character_id)
                row = next(row for row in rows if len(row["context"]) >= 4
                           and all(not line.startswith("旁白：") for line in row["context"][-4:]))
                conversation = tuple(PromptMessage("assistant" if line.startswith("角色：") else "user",
                                      line.partition("：")[2]) for line in row["context"][-4:])
                hits = await scene_runtime.search(SceneQuery(scene_character.character_id, conversation))
                assert hits and hits[0].score > .99
                print(f"真实场景检索正例：{hits[0].scene_id}，score={hits[0].score:.4f}", flush=True)
            actual_counter = TokenizerTokenCounter()
            await asyncio.to_thread(actual_counter.load_directory, settings.reply_tokenizer_path)
            counter = DeepSeekApproximateCounter(actual_counter) if approximate else actual_counter
            yield settings, memory_runtime, SimpleNamespace(service=reply_service, scene_recall=scene_runtime.recall if scene_runtime is not None else None), counter
    finally:
        # 原库文件内容必须保持原样。不能同时让其他程序修改源记忆库。
        assert hashlib.sha256((original_memory / "meta.json").read_bytes()).hexdigest() == before["meta"]
        source_db = original_memory / "collection" / COLLECTION / "storage.sqlite"
        assert hashlib.sha256(source_db.read_bytes()).hexdigest() == before["storage"]
        if scene_before:
            assert hashlib.sha256((scene_source / "meta.json").read_bytes()).hexdigest() == scene_before["meta"]
            assert hashlib.sha256((scene_source / "collection/character_scenes/storage.sqlite").read_bytes()).hexdigest() == scene_before["storage"]


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("好久没找你了，想见你了", id="reunion"),
        pytest.param("这一个月都在赶一个项目，工作都搁置了，好累，总算告一段落了", id="long_project_wrapup"),
    ],
)
async def test_real_chat_normal_path(question, live_resources, tmp_path):
    settings, memory_runtime, reply_runtime, counter = live_resources

    # 1. 每个场景独立临时库，不打开 memory_record/database/rna_companion.db。
    database = SQLiteDatabase(tmp_path / "chat.sqlite3")
    app = None
    try:
        await database.initialize()
        character_repository = SQLiteCharacterRepository(database)
        await character_repository.register_manifest_characters(ROOT)
        character_id = await character_repository.default_character_id()
        assert character_id, "未登记角色"
        repository = SQLiteGenerationRepository(database)
        messages = MessageService(SQLiteChatRepository(database))
        query = GenerationQueryService(repository, messages)
        memory = MemoryRecallService(
            memory_runtime, RecallReaderFactory(memory_runtime.qdrant, memory_runtime.encoder, database=database, token_counter=counter),
            MemoryRecallResourceLoader.load(),
            MemoryRecallConfig(
                selection_model=settings.memory_selection_model,
                summary_model=settings.memory_summary_model,
                total_seconds=settings.memory_total_seconds,
                target_seconds=settings.memory_target_seconds,
                selection_limit=settings.memory_selection_seconds,
            ),
            # 缓存也仅保存在本场景临时聊天库中。
            memory_cache=MemoryCacheService(SQLiteRecallCache(database)), token_counter=counter,
        )
        preparation = GenerationPreparationService(
            ContextLoader(SQLiteContextReadRepository(database)), memory,
            prompt_resources=FileReplyPromptResourceReader(database, ROOT),
            prompt_builder=PromptBuilder(counter, anchor_position=settings.reply_anchor_position,
                                         example_format=settings.reply_example_format),
            scene_recall=reply_runtime.scene_recall,
        )
        completion = GenerationCompletionService(repository, reply_runtime.service, counter)
        app = create_app()
        app.dependency_overrides[get_message_service] = lambda: messages
        app.dependency_overrides[get_generation_query_service] = lambda: query

        # 不启动默认 lifespan，防止默认容器连接正式聊天库并运行 worker。
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async with asyncio.timeout(float(os.getenv("RNA_LIVE_TIMEOUT_SECONDS", "240"))):
                print(f"\n用户：{question}\n临时聊天库：{database.path}", flush=True)

                # 2. 真实 HTTP 用户输入 → 校验 → T1 提交。
                identifier = uuid4().hex
                response = await client.post(
                    f"/api/v1/conversations/live_{identifier}/messages",
                    json={"request_id": f"live_req_{identifier}",
                          "character_id": character_id, "content": question},
                )
                assert response.status_code == 202, response.text
                saved = response.json()
                assert saved["save_state"] == "committed"

                # 3. T2-A 固定输入，再从临时库加载工作上下文。
                ticket = await repository.claim_job(saved["job_id"])
                assert ticket is not None
                context = await preparation.load_context(ticket)
                assert context.current_input.user_messages[0].content == question

                # 4. DeepSeek 记忆 Agent → 按需总结 → 构建 GLM 提示词。
                # “想见你”无需记忆也正常，不能强制每个场景都检索出历史。
                frozen = await preparation.assemble(ticket, context)
                audit = frozen.memory_audit
                assert audit.status in ("ready", "empty")
                assert frozen.frozen_prompt.budget_checked is True
                assert frozen.frozen_prompt.messages[-1].content == question
                if audit.status == "ready":
                    assert audit.source_refs
                    assert audit.summary_status == "summarized"
                    assert frozen.frozen_prompt.memory_summary.strip()
                else:
                    assert not audit.source_refs
                    assert not frozen.frozen_prompt.memory_summary
                print(f"记忆状态：{audit.status}；选择理由：{audit.reason_code}", flush=True)
                print(f"工具轨迹：{[entry.get('name') for entry in audit.trajectory]}", flush=True)
                print(f"记忆总结：{frozen.frozen_prompt.memory_summary or '本轮无需历史记忆'}", flush=True)

                # 5. T2-B 提交后，使用数据库内冻结材料实际调用 GLM。
                assert await repository.freeze_prompt(frozen) is True
                committed = await repository.get_attempt(ticket.attempt_id)
                generated = await reply_runtime.service.generate(
                    committed.frozen_prompt,
                    expected_prompt_hash=ReplyFingerprint.prompt_hash(committed.frozen_prompt.messages),
                )
                candidate = generated.candidate
                assert candidate.reply.response_zh.strip()
                assert candidate.reply.visual is not None
                if settings.reply_scene_enabled:
                    trace = candidate.generation_trace
                    assert trace is not None
                    assert trace["request_id"]
                    assert trace["schema"] == "single-stage-scene-reply-v1"
                    print(f"单阶段回复：{candidate.reply.response_zh}", flush=True)
                    print(f"场景示例命中：{[(hit['scene_id'], round(hit['score'], 4)) for hit in trace['scenes']]}", flush=True)
                    from dataclasses import asdict
                    assert asdict(candidate.reply.visual) == trace["reply_visual"]

                # 6. 候选保存后仍不是正式回复。
                accept_ticket = ReplyAcceptanceTicket(
                    ticket.attempt_id, ticket.input_version, ticket.active_token,
                    candidate.prompt_hash, candidate.candidate_hash,
                )
                assert await repository.save_candidate(accept_ticket, candidate) is True
                pending = await client.get(f"/api/v1/generation-jobs/{saved['job_id']}")
                assert pending.status_code == 200
                assert pending.json()["status"] == "need_accept"
                assert pending.json()["reply"] is None

                # 7. tokenizer 计数 → T3 提交，不重复调用 GLM。
                accepted = await completion.complete(ticket.attempt_id)
                assert accepted is not None
                async with database.db_session() as session:
                    turn = await session.get(TurnRow, saved["turn_id"])
                    assert turn.turn_state == "complete"
                    assert turn.accepted_message_id == accepted.message_id
                    assert turn.turn_token_count > 0
                    assert turn.tokenizer_version == counter.tokenizer_version
                    chat_session = await session.get(SessionRow, turn.session_id)
                    assert chat_session.session_progress_tokens == turn.turn_token_count
                    assert (await session.get(GenerationJobRow, saved["job_id"])).state == "done"
                    attempt = await session.get(GenerationAttemptRow, ticket.attempt_id)
                    assert attempt.attempt_state == "accepted"
                    assert attempt.memory_audit_json is not None
                    stored = (await session.scalars(select(MessageRow).where(
                        MessageRow.turn_id == turn.turn_id,
                    ).order_by(MessageRow.message_seq))).all()
                    assert [item.role for item in stored] == ["user", "character"]
                    assert stored[0].content == question
                    assert stored[1].content == candidate.reply.response_zh

                # 8. 经真实 HTTP 路由从临时库读取正式输出。
                final = await client.get(f"/api/v1/generation-jobs/{saved['job_id']}")
                assert final.status_code == 200
                result = final.json()
                assert result["status"] == "done"
                assert result["reply"]["message_id"] == accepted.message_id
                assert result["reply"]["content"] == candidate.reply.response_zh
                assert result["reply"]["visual"] is not None
                print(f"正式回复：{result['reply']['content']}", flush=True)
    finally:
        if app is not None:
            app.dependency_overrides.clear()
        await database.close()
