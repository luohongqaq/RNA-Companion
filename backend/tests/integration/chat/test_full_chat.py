"""一轮聊天的正常路径：HTTP 输入 → T1 → T2 → 记忆 Agent → T3 → HTTP 输出。

运行：python -m pytest tests/integration/chat/test_full_chat.py -v -s

真实组件：HTTP 路由、SQLite/迁移/仓储、上下文读取、角色文件读取、记忆
Agent/工具校验/总结编排、PromptBuilder、回复解析、采用及正式回复查询。
替代组件：记忆存储适配器、两个外部模型、tokenizer。不会联网或读取正式数据。
为教学逐步调用生产阶段入口，不启动 worker，不验证自动调度或应用 lifespan。
"""

import copy
import hashlib
import json
from contextlib import contextmanager
from time import perf_counter
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from module.memory_recall.agent.core.processing.selection.service import MemorySelectionService
from module.memory_recall.agent.core.processing.summary.service import MemorySummaryService
from api.app import create_app
from api.dependencies import get_generation_query_service, get_message_service
from module.chat.conversation.core.processing.messages import MessageService
from module.chat.reply.core.processing.context_loader import ContextLoader
from module.chat.reply.core.processing.completion import GenerationCompletionService
from module.chat.reply.core.processing.preparation import GenerationPreparationService
from module.chat.reply.core.processing.prompt_builder import PromptBuilder
from module.chat.reply.core.domain.fingerprint import ReplyFingerprint
from module.chat.reply.core.processing.service import GenerationReplyService
from module.chat.reply.core.domain.contracts import ReplyAcceptanceTicket
from module.chat.reply.core.processing.query import GenerationQueryService
from module.memory_recall.agent.core.processing.config.pipeline import MemoryRecallConfig
from module.memory_recall.agent.core.processing.service import MemoryRecallService
from module.memory_recall.agent.core.domain.model import MemoryModelResult
from module.memory_recall.retrieval.core.domain.tools.read import ReadEntry, ReadResult
from module.memory_recall.retrieval.core.domain.tools.search import SearchCoverage, SearchItem, SearchResult
from module.memory_recall.retrieval.core.domain.tools.common import TimeContext
from module.memory_recall.agent.repository.resources import MemoryRecallResourceLoader
from module.chat.reply.repository.prompt_resources import FileReplyPromptResourceReader
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from module.chat.reply.repository.context_sqlite import SQLiteContextReadRepository
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from storage.sqlite.models import (
    CharacterRow, GenerationAttemptRow, GenerationJobRow, MessageRow,
    RequestReceiptRow, SessionRow, TurnRow,
)
from module.chat.reply.repository.llm import ReplyModelRepository


QUESTION = "上次我为什么说想减少每天的学习任务？"
MEMORY_TEXT = "用户上次表示每天安排六项学习任务很疲惫，希望减为三项。"
SUMMARY = "用户上次因每天六项学习任务带来的疲惫，考虑将任务减为三项。"
ANSWER = "你上次说每天安排六项学习任务有些累，所以想减为三项，让自己轻松一点。"


class StageTimer:
    """只记录耗时，不设置容易受机器负载影响的性能通过阈值。"""

    def __init__(self):
        self.seconds = {}

    @contextmanager
    def measure(self, name):
        started = perf_counter()
        try:
            yield
        finally:
            self.seconds[name] = self.seconds.get(name, 0) + perf_counter() - started

    def report(self):
        print("\n一轮聊天计时（固定模型响应；单位 ms）")
        for name, seconds in self.seconds.items():
            print(f"  {name}: {seconds * 1000:.3f}")
        print("  记忆选择/总结包含在提示词准备中；总耗时包含阶段断言，勿重复相加。")


class FixedMemoryReader:
    """替代 Qdrant/正式记忆存储；提供一条确定的历史记忆。

    仍通过真实 MemoryReadSession 签发句柄，真实 Agent 负责校验和选材。
    这里只验证业务联动，不证明 Qdrant、Markdown 或正式版本目录正确。
    """

    def __init__(self):
        self.calls = []
        self.content_hash = hashlib.sha256(MEMORY_TEXT.encode()).hexdigest()

    async def temporal_coverage(self):
        return {"metadata_scan_complete": True}

    async def search(self, query, ledger):
        self.calls.append("search")
        assert query.mode == "grep"
        assert "学习" in query.literal_terms
        card = ledger.register_candidate("daily_test_001")
        sections = {"memorable_moments": MEMORY_TEXT}
        handle = ledger.register_read(
            "daily_test_001", sections, 1, self.content_hash,
            kind="daily", title="学习安排", conversation_time="2026-10-25T10:00:00+08:00",
        )
        return SearchResult(
            status="ok",
            coverage=SearchCoverage(scope_handle="scope_hot", literal_scan_complete=True),
            items=[SearchItem(
                card_handle=card, read_handle=handle, kind="daily", title="学习安排",
                matched_by=["grep"], sections=sections,
                time_context=TimeContext(conversation_time="2026-10-25T10:00:00+08:00"),
            )],
        )

    async def read(self, query, ledger):
        self.calls.append("read")
        card = query.card_handles[0]
        assert ledger.cards[card] == "daily_test_001"
        sections = {"memorable_moments": MEMORY_TEXT}
        handle = ledger.register_read(
            "daily_test_001", sections, 1, self.content_hash,
            kind="daily", title="学习安排", conversation_time="2026-10-25T10:00:00+08:00",
        )
        return ReadResult(status="ok", reads=[ReadEntry(
            read_handle=handle, card_handle=card, kind="daily",
            title="学习安排", sections=sections,
            time_context=TimeContext(conversation_time="2026-10-25T10:00:00+08:00"),
        )])


class FixedMemoryReaderFactory:
    def __init__(self):
        self.reader = FixedMemoryReader()
        self.character_ids = []

    def create(self, character_id):
        self.character_ids.append(character_id)
        return self.reader


class ScriptedMemoryModel:
    """只替代模型输出：依次发出 search → read → finish，随后返回总结。

    句柄从真实工具返回中取得，不能自行编造；业务 Agent 和总结流程不替换。
    """

    def __init__(self):
        self.calls = []
        self.selection_calls = 0

    async def complete(self, request):
        self.calls.append(copy.deepcopy(request))
        if request.model == "test-summary":
            assert MEMORY_TEXT in request.messages[-1]["content"]
            return MemoryModelResult(
                message={"role": "assistant", "content": SUMMARY}, finish_reason="stop",
            )

        assert request.model == "test-selection"
        self.selection_calls += 1
        if self.selection_calls == 1:
            assert any(QUESTION in (item.get("content") or "") for item in request.messages)
            name = "search_memories"
            arguments = {
                "scope_handle": "scope_hot", "purpose": "查找之前减少学习任务的原因",
                "mode": "grep", "literal_terms": ["学习"], "kinds": ["daily"],
            }
        elif self.selection_calls == 2:
            tool_message = next(item for item in reversed(request.messages) if item["role"] == "tool")
            result = json.loads(tool_message["content"])
            assert result["status"] == "ok", result
            name = "read_memories"
            arguments = {
                "purpose": "读取相关历史正文",
                "card_handles": [result["items"][0]["card_handle"]],
            }
        elif self.selection_calls == 3:
            tool_message = next(item for item in reversed(request.messages) if item["role"] == "tool")
            result = json.loads(tool_message["content"])
            assert result["status"] == "ok", result
            name = "finish_selection"
            arguments = {
                "reason_code": "selected",
                "selected": [{
                    "read_handle": result["reads"][0]["read_handle"],
                    "section_keys": ["memorable_moments"],
                }],
                "unresolved": [],
            }
        else:
            raise AssertionError("正常路径不应额外请求记忆选择模型")
        return MemoryModelResult(message={
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": f"call_{self.selection_calls}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }],
        }, finish_reason="tool_calls")


class ScriptedReplyClient:
    """模拟 SDK 的 chat.completions.create；仍执行生产 GenerationReplyService 解析。"""

    def __init__(self):
        self.chat = self
        self.completions = self
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        assert any(SUMMARY in item["content"] for item in kwargs["messages"])
        assert kwargs["messages"][-1] == {"role": "user", "content": QUESTION}
        return SimpleNamespace(
            id="reply_request_test", usage=None,
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=json.dumps({
                    "response_zh": ANSWER,
                    "visual": {"pose_name": "正面自然站姿", "expression_name": "微笑·温和"},
                }, ensure_ascii=False)),
            )],
        )


class FixedTokenCounter:
    """仅验证计数接口联动；20 是测试桩值，不是真实 token 数。"""

    tokenizer_version = "fixed-test-counter"

    def __init__(self):
        self.calls = []

    def count_text(self, text):
        return len(text)

    def count_messages(self, messages):
        self.calls.append(tuple(messages))
        return 20


@pytest_asyncio.fixture
async def database(tmp_path):
    database = SQLiteDatabase(tmp_path / "full_chat.sqlite3")
    try:
        await database.initialize()
        # 真实角色读取器要求三份文件；只在临时目录准备最小角色卡。
        directory = tmp_path / "character"
        directory.mkdir()
        titles = ("身份", "核心品格", "关系立场", "人格使用边界", "注意力与认知", "日常陪伴基调")
        card = "\n\n".join(f"## {title}\n温和陪伴，尊重用户的原话。" for title in titles)
        (directory / "角色卡.md").write_text(card, encoding="utf-8")
        (directory / "系统提示词.md").write_text("依据输入与历史记忆回答，并输出规定 JSON。", encoding="utf-8")
        (directory / "语气提示词.md").write_text("语气自然。", encoding="utf-8")
        async with database.transaction() as session:
            session.add(CharacterRow(
                character_id="character_test", name="测试桌宠", character_setting="温和陪伴",
                card_path=str(directory),
            ))
        yield database
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_one_chat_from_http_input_to_formal_reply(database, monkeypatch):
    timer = StageTimer()

    # 0. 组装真实服务。只替换外部模型、记忆存储适配器和 tokenizer。
    with timer.measure("初始化服务（不计入整轮）"):
        chat_repository = SQLiteChatRepository(database)
        repository = SQLiteGenerationRepository(database)
        messages = MessageService(chat_repository)
        query = GenerationQueryService(repository, messages)
        readers = FixedMemoryReaderFactory()
        memory_model = ScriptedMemoryModel()
        reply_client = ScriptedReplyClient()
        counter = FixedTokenCounter()
        memory = MemoryRecallService(
            memory_model, readers, MemoryRecallResourceLoader.load(),
            MemoryRecallConfig(selection_model="test-selection", summary_model="test-summary", prefetch_enabled=False),
            token_counter=counter,
        )
        preparation = GenerationPreparationService(
            ContextLoader(SQLiteContextReadRepository(database)), memory,
            prompt_resources=FileReplyPromptResourceReader(database),
            prompt_builder=PromptBuilder(counter),
        )
        reply = GenerationReplyService(ReplyModelRepository(reply_client))
        completion = GenerationCompletionService(repository, reply, counter)

        # 包装真实函数只为计时；不替换它们的执行结果。monkeypatch 自动恢复。
        original_selection = MemorySelectionService.select
        original_summary = MemorySummaryService.summarize

        async def timed_selection(**kwargs):
            with timer.measure("记忆 Agent 选择（含搜索/读取工具）"):
                result = await original_selection(**kwargs)
            assert result["reason_code"] == "selected", result
            return result

        async def timed_summary(self, **kwargs):
            with timer.measure("记忆独立总结"):
                return await original_summary(self, **kwargs)

        monkeypatch.setattr(MemorySelectionService, "select", staticmethod(timed_selection))
        monkeypatch.setattr(MemorySummaryService, "summarize", timed_summary)

        app = create_app()
        app.dependency_overrides[get_message_service] = lambda: messages
        app.dependency_overrides[get_generation_query_service] = lambda: query

    # ASGITransport 不自动启动 lifespan：资源由 fixture 管理，避免加载正式配置。
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            with timer.measure("整轮总耗时（含查询与阶段断言）"):
                # 1. 用户输入经真实 HTTP 路由、Pydantic、MessageService 和 T1 保存。
                with timer.measure("T1 HTTP 输入与保存"):
                    response = await client.post(
                        "/api/v1/conversations/conversation_test/messages",
                        json={"request_id": "request_test_001", "character_id": "character_test", "content": QUESTION},
                    )
                assert response.status_code == 202, response.text
                saved = response.json()
                assert saved["save_state"] == "committed"
                job_id, turn_id = saved["job_id"], saved["turn_id"]
                async with database.db_session() as session:
                    user_message = await session.get(MessageRow, saved["message_id"])
                    assert user_message.content == QUESTION
                    assert user_message.role == "user"
                    receipt = await session.get(RequestReceiptRow, ("request_test_001", "send_message"))
                    assert receipt is not None
                    assert (await session.get(GenerationJobRow, job_id)).state == "need_prepare"

                # 2. T2-A：取得生成资格，固定当前消息 ID 与历史读取范围。
                with timer.measure("T2-A 领取任务"):
                    ticket = await repository.claim_job(job_id)
                assert ticket is not None
                assert ticket.input_boundary.current_turn.message_ids == (saved["message_id"],)

                # 3. 根据票据从真实 SQLite 加载上下文。第一轮没有工作摘要/历史轮。
                with timer.measure("工作上下文加载"):
                    context = await preparation.load_context(ticket)
                assert context.working_snapshot is None
                assert context.complete_turns == ()
                assert context.current_input.user_messages[0].content == QUESTION

                # 4. 读取角色文件 → Agent 搜索/读取/选材 → 独立总结 → 构建提示词。
                with timer.measure("提示词准备总计（包含记忆选择/总结）"):
                    freeze_ticket = await preparation.assemble(ticket, context)
                frozen = freeze_ticket.frozen_prompt
                assert readers.character_ids == ["character_test"]
                assert readers.reader.calls == ["search", "read"]
                assert [call.model for call in memory_model.calls] == [
                    "test-selection", "test-selection", "test-selection", "test-summary",
                ]
                assert frozen.memory_summary == SUMMARY
                assert frozen.memory_manifest.daily[0].memory_id == "daily_test_001"
                assert freeze_ticket.memory_audit.source_cards
                assert frozen.messages[-1].content == QUESTION
                assert frozen.budget_checked is True

                # 5. T2-B 提交后，模型才能使用冻结提示词。
                with timer.measure("T2-B 冻结提示词"):
                    assert await repository.freeze_prompt(freeze_ticket) is True
                async with database.db_session() as session:
                    job = await session.get(GenerationJobRow, job_id)
                    attempt = await session.get(GenerationAttemptRow, ticket.attempt_id)
                    assert job.state == "need_generate"
                    assert attempt.frozen_prompt_json is not None
                    assert attempt.memory_audit_json is not None
                    assert attempt.candidate_json is None

                # 从数据库重新加载，而非直接使用尚未提交的内存材料。
                committed = await repository.get_attempt(ticket.attempt_id)
                with timer.measure("回复生成与 JSON 解析（固定模型响应）"):
                    generated = await reply.generate(
                        committed.frozen_prompt,
                        expected_prompt_hash=ReplyFingerprint.prompt_hash(committed.frozen_prompt.messages),
                    )
                candidate = generated.candidate
                assert candidate.reply.response_zh == ANSWER

                # 6. 候选提交后仍不是正式回复，HTTP 查询必须暂时返回 reply=null。
                accept_ticket = ReplyAcceptanceTicket(
                    ticket.attempt_id, ticket.input_version, ticket.active_token,
                    candidate.prompt_hash, candidate.candidate_hash,
                )
                with timer.measure("候选保存"):
                    assert await repository.save_candidate(accept_ticket, candidate) is True
                pending = await client.get(f"/api/v1/generation-jobs/{job_id}")
                assert pending.status_code == 200
                assert pending.json()["status"] == "need_accept"
                assert pending.json()["reply"] is None

                # 7. 采用编排读取持久候选，统计本轮 token，再执行真实 T3。
                with timer.measure("T3 计数与正式采用"):
                    accepted = await completion.complete(ticket.attempt_id)
                assert accepted is not None
                assert accepted.content == ANSWER
                assert len(reply_client.calls) == 1  # 有候选后不会重复调用回复模型。
                assert [(item.role, item.content) for item in counter.calls[-1]] == [
                    ("user", QUESTION), ("assistant", ANSWER),
                ]
                async with database.db_session() as session:
                    turn = await session.get(TurnRow, turn_id)
                    assert turn.turn_state == "complete"
                    assert turn.accepted_message_id == accepted.message_id
                    assert turn.turn_token_count == 20  # 固定测试计数，不是真实 tokenizer 数值。
                    chat_session = await session.get(SessionRow, turn.session_id)
                    assert chat_session.session_progress_tokens == 20
                    assert (await session.get(GenerationJobRow, job_id)).state == "done"
                    assert (await session.get(GenerationAttemptRow, ticket.attempt_id)).attempt_state == "accepted"
                    assert await session.scalar(select(func.count()).select_from(MessageRow)) == 2

                # 8. 最终输出通过真实 HTTP 查询从 SQLite 正式消息中读取。
                with timer.measure("HTTP 查询最终正式输出"):
                    final_response = await client.get(f"/api/v1/generation-jobs/{job_id}")
                assert final_response.status_code == 200
                result = final_response.json()
                assert result["status"] == "done"
                assert result["reply"]["message_id"] == accepted.message_id
                assert result["reply"]["content"] == ANSWER
                assert result["reply"]["visual"]["pose_name"]["pose_name"] == "正面自然站姿"
                assert result["reply"]["visual"]["expression_name"]["expression_name"] == "微笑·温和"
                print(f"\n用户输入：{QUESTION}\n记忆总结：{SUMMARY}\n正式输出：{result['reply']['content']}")
        finally:
            app.dependency_overrides.clear()
            timer.report()
