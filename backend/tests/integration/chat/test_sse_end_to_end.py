"""真实本地 HTTP：发送 → Worker → T3 → SSE → 断线补发 → 失败重试。

使用临时 SQLite、生产服务和真实 Uvicorn；仅模型、记忆适配器与计数器使用测试桩。
"""
import asyncio
from contextlib import aclosing, contextmanager
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
import uvicorn

from api.app import create_app
from module.chat.conversation.core.processing.events import ChatEventService
from module.chat.conversation.core.processing.messages import MessageService
from module.chat.reply.core.processing.context_loader import ContextLoader
from module.chat.reply.core.processing.completion import GenerationCompletionService
from module.chat.reply.core.processing.preparation import GenerationPreparationService
from module.chat.reply.core.processing.prompt_builder import PromptBuilder
from module.chat.reply.core.processing.service import GenerationReplyService
from module.shared.jobs.config.retry import RetryPolicy
from module.chat.reply.core.processing.query import GenerationQueryService
from module.chat.reply.core.scheduling.worker import GenerationWorker
from module.chat.reply.core.scheduling.workflow import GenerationWorkflow
from module.memory_recall.agent.core.processing.config.pipeline import MemoryRecallConfig
from module.memory_recall.agent.core.processing.service import MemoryRecallService
from module.memory_recall.agent.repository.resources import MemoryRecallResourceLoader
from module.chat.reply.repository.prompt_resources import FileReplyPromptResourceReader
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from module.chat.reply.repository.context_sqlite import SQLiteContextReadRepository
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from storage.sqlite.models import ChatEventRow, GenerationJobRow, MessageRow, RequestReceiptRow
from test_full_chat import (
    ANSWER, QUESTION, FixedMemoryReaderFactory, FixedTokenCounter,
    ScriptedMemoryModel, ScriptedReplyClient, database,
)
from module.chat.reply.repository.llm import ReplyModelRepository


class ReusableMemoryModel(ScriptedMemoryModel):
    async def complete(self, request):
        result = await super().complete(request)
        if request.model == "test-summary":
            self.selection_calls = 0
        return result


class ControlledReplyClient(ScriptedReplyClient):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Queue()
        self.gates = []
        self.failures = set()

    async def create(self, **kwargs):
        gate = asyncio.Event()
        self.gates.append(gate)
        number = len(self.gates)
        self.started.put_nowait(number)
        await gate.wait()
        if number in self.failures:
            raise TimeoutError("模拟回复模型超时")
        return await super().create(**kwargs)


class TrackedEvents(ChatEventService):
    def __init__(self, repository):
        super().__init__(repository, poll_interval=0.01, heartbeat_interval=0.05)
        self.active = 0

    async def subscribe(self, cursor=0):
        self.active += 1
        try:
            async with aclosing(super().subscribe(cursor)) as events:
                async for event in events:
                    yield event
        finally:
            self.active -= 1


class TestServer(uvicorn.Server):
    __test__ = False

    @contextmanager
    def capture_signals(self):
        yield  # 测试服务器不接管 pytest 的进程信号。


async def wait_until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def live_chat(database):
    chat = SQLiteChatRepository(database)
    repository = SQLiteGenerationRepository(database)
    model, memory_model = ControlledReplyClient(), ReusableMemoryModel()
    counter = FixedTokenCounter()
    memory = MemoryRecallService(
        memory_model, FixedMemoryReaderFactory(), MemoryRecallResourceLoader.load(),
        MemoryRecallConfig(selection_model="test-selection", summary_model="test-summary", prefetch_enabled=False),
        token_counter=counter,
    )
    preparation = GenerationPreparationService(
        ContextLoader(SQLiteContextReadRepository(database)), memory,
        prompt_resources=FileReplyPromptResourceReader(database), prompt_builder=PromptBuilder(counter),
    )
    completion = GenerationCompletionService(repository, GenerationReplyService(ReplyModelRepository(model)), counter)
    worker = GenerationWorker(repository, GenerationWorkflow(
        repository, preparation, completion, retry_policy=RetryPolicy(max_retries=0),
    ), poll_interval=0.01)
    messages = MessageService(chat, notify_generation=worker.notify)
    events = TrackedEvents(chat)
    app = create_app()
    app.state.container = SimpleNamespace(
        message_service=messages, chat_event_service=events,
        generation_query_service=GenerationQueryService(repository, messages, notify_generation=worker.notify),
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", loop="asyncio",
                            http="h11", ws="none", access_log=False, log_level="error", timeout_graceful_shutdown=2)
    server = TestServer(config)
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async def ready():
            if serving.done():
                await serving
            return server.started
        await wait_until(ready)
        await worker.start()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                                     timeout=httpx.Timeout(5)) as client:
            yield SimpleNamespace(client=client, database=database, model=model,
                                  memory_model=memory_model, events=events)
    finally:
        await worker.stop()
        async def requests_drained():
            return not server.server_state.tasks
        await wait_until(requests_drained)
        # SQLAlchemy 的取消屏蔽清理可能稍晚归还连接；等真实释放而非依赖瞬时检查。
        async def database_drained():
            return database.engine.pool.checkedout() == 0
        await wait_until(database_drained)
        assert database.engine.pool.checkedout() == 0
        # 等待客户端关闭的 TCP 连接完成注销，再关闭监听端口。
        async def connections_drained():
            return not server.server_state.connections
        await wait_until(connections_drained)
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=5)
        sock.close()


async def frame(lines):
    result = {}
    async with asyncio.timeout(5):
        async for line in lines:
            if line == "":
                if result:
                    return result
            elif line.startswith(":"):
                result["heartbeat"] = True
            else:
                key, _separator, value = line.partition(":")
                result[key] = value.lstrip(" ")
    raise AssertionError("事件流意外结束")


async def reply_event(lines):
    while True:
        item = await frame(lines)
        if item.get("event") == "reply_committed":
            return int(item["id"]), json.loads(item["data"])


async def send_message(client, number):
    path = f"/api/v1/conversations/sse_conv_{number}/messages"
    body = {"request_id": f"sse_request_{number}", "character_id": "character_test", "content": QUESTION}
    response = await client.post(path, json=body)
    assert response.status_code == 202, response.text
    assert response.json()["save_state"] == "committed"
    return response.json(), path, body


async def wait_job(client, job_id, status):
    result = None
    async def matches():
        nonlocal result
        response = await client.get(f"/api/v1/generation-jobs/{job_id}")
        assert response.status_code == 200
        result = response.json()
        return result["status"] == status
    await wait_until(matches)
    return result


@pytest.mark.asyncio
async def test_sse_chat_full_acceptance(live_chat):
    test, checks = live_chat, []
    client = test.client
    async with client.stream("GET", "/api/v1/events?after=0") as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        lines = stream.aiter_lines()
        assert (await frame(lines))["retry"] == "3000"
        saved, path, body = await send_message(client, 1)
        assert await asyncio.wait_for(test.model.started.get(), 5) == 1
        pending = await client.get(f"/api/v1/generation-jobs/{saved['job_id']}")
        assert pending.json()["status"] == "need_generate"
        async with test.database.db_session() as session:
            assert await session.scalar(select(func.count()).select_from(ChatEventRow)) == 0
        checks.append("模型仍在等待时，发送接口已返回保存回执；无提前发布事件")
        test.model.gates[0].set()
        first_id, first = await reply_event(lines)
        done = await wait_job(client, saved["job_id"], "done")
        assert first["reply"] == done["reply"]
        assert first["reply"]["content"] == ANSWER
        assert first["job_id"] == saved["job_id"]
        history = (await client.get(path)).json()["items"]
        assert [item["role"] for item in history] == ["user", "character"]
        assert history[-1]["message_id"] == first["reply"]["message_id"]
        checks.append("真实 Worker 完成准备、模型解析、T3，SSE 与查询及历史回复一致")
        assert (await client.post(path, json=body)).json() == saved
        assert (await frame(lines)).get("heartbeat")
        checks.append("重复发送不重复落库或推送；空闲连接收到心跳")

    async def no_subscriber():
        return test.events.active == 0
    await wait_until(no_subscriber)
    async with client.stream("GET", f"/api/v1/events?after={first_id}") as stream:
        lines = stream.aiter_lines()
        assert (await frame(lines))["retry"] == "3000"
        second, second_path, _body = await send_message(client, 2)
        assert await asyncio.wait_for(test.model.started.get(), 5) == 2
        # 模型调用期间关闭 SSE，然后允许后台生成继续。
    await wait_until(no_subscriber)
    test.model.gates[1].set()
    second_done = await wait_job(client, second["job_id"], "done")
    checks.append("SSE 在模型调用中断开后，订阅清理完成，后台仍完成回复")

    async with client.stream("GET", "/api/v1/events?after=0", headers={"Last-Event-ID": str(first_id)}) as stream:
        lines = stream.aiter_lines()
        second_id, replay = await reply_event(lines)
        assert second_id > first_id
        assert replay["job_id"] == second["job_id"]
        assert replay["reply"] == second_done["reply"]
        assert (await frame(lines)).get("heartbeat")
        checks.append("重连优先使用 Last-Event-ID，只补发断线期间的回复")
        # 再验证失败后手动重试，复用冻结材料，最终通过同一 SSE 收到正式回复。
        test.model.failures.add(3)
        third, third_path, _body = await send_message(client, 3)
        assert await asyncio.wait_for(test.model.started.get(), 5) == 3
        test.model.gates[2].set()
        failed_job = await wait_job(client, third["job_id"], "failed")
        failed_frame = await frame(lines)
        while failed_frame.get("event") is None:
            failed_frame = await frame(lines)
        assert failed_frame["event"] == "generation_failed"
        failed_id = int(failed_frame["id"])
        failed_data = json.loads(failed_frame["data"])
        assert failed_data["job_id"] == third["job_id"]
        assert failed_data["error"] == failed_job["error"]
        assert failed_data["manual_retry_count"] == 0
        async with test.database.db_session() as session:
            assert await session.scalar(select(func.count()).select_from(ChatEventRow)) == 3
        calls_before_retry = len(test.memory_model.calls)
        response = await client.post(f"/api/v1/generation-jobs/{third['job_id']}/retry")
        assert response.status_code == 202
        assert response.json()["status"] == "need_generate"
        assert await asyncio.wait_for(test.model.started.get(), 5) == 4
        assert len(test.memory_model.calls) == calls_before_retry
        test.model.gates[3].set()
        third_id, retried = await reply_event(lines)
        assert third_id > failed_id > second_id
        assert retried["job_id"] == third["job_id"]
        assert retried["reply"]["content"] == ANSWER
        assert (await frame(lines)).get("heartbeat")
        checks.append("最终失败推送失败事件；手动重试跳过记忆准备，完成后推送正式回复")
    await wait_until(no_subscriber)
    for message_path in (path, second_path, third_path):
        assert len((await client.get(message_path)).json()["items"]) == 2
    async with test.database.db_session() as session:
        for table, expected in ((MessageRow, 6), (ChatEventRow, 4),
                                (GenerationJobRow, 3), (RequestReceiptRow, 3)):
            assert await session.scalar(select(func.count()).select_from(table)) == expected
    assert len(test.model.gates) == 4 and len(test.model.calls) == 3
    for check in checks:
        print(f"PASS: {check}")
