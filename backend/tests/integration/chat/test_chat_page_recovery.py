"""页面恢复验收：真实 HTTP + Worker + 临时数据库，不依赖真实模型或前端。"""
import asyncio
import json

import httpx
import pytest

from test_sse_end_to_end import (
    ANSWER, database, frame, live_chat, send_message, wait_job, wait_until,
)


async def next_event(lines):
    """跳过连接控制帧和心跳，但不跳过任何业务事件。"""
    async with asyncio.timeout(5):
        while True:
            item = await frame(lines)
            if "event" in item:
                return item["event"], int(item["id"]), json.loads(item["data"])


async def snapshot(client, message_path):
    history, jobs = await asyncio.gather(
        client.get(message_path), client.get(message_path.removesuffix("/messages") + "/generation-jobs"),
    )
    assert history.status_code == jobs.status_code == 200
    return history.json()["items"], jobs.json()["items"]


def reopened_client(test):
    # 新建 HTTP 客户端，模拟页面/前端重新打开；后端进程继续运行。
    return httpx.AsyncClient(base_url=test.client.base_url, trust_env=False, timeout=5)


async def disconnect_subscription(test):
    async with test.client.stream("GET", "/api/v1/events") as stream:
        assert (await frame(stream.aiter_lines()))["retry"] == "3000"
    async def disconnected():
        return test.events.active == 0
    await wait_until(disconnected)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["done", "failed"])
async def test_reopen_after_background_finishes(live_chat, outcome):
    test = live_chat
    saved, path, _ = await send_message(test.client, 1)
    assert await asyncio.wait_for(test.model.started.get(), 5) == 1
    await disconnect_subscription(test)
    assert test.events.active == 0
    if outcome == "failed":
        test.model.failures.add(1)
    test.model.gates[0].set()
    finished = await wait_job(test.client, saved["job_id"], outcome)

    async with reopened_client(test) as client:
        history, jobs = await snapshot(client, path)
        if outcome == "done":
            assert [item["role"] for item in history] == ["user", "character"]
            assert history[-1]["content"] == ANSWER
            assert history[-1]["message_id"] == finished["reply"]["message_id"]
            assert jobs == []  # 已完成回复由历史消息恢复，不作为待处理任务返回。
        else:
            assert len(history) == 1 and history[0]["role"] == "user"
            assert len(jobs) == 1 and jobs[0] == finished
            assert jobs[0]["manual_retry_count"] == 0
        async with client.stream("GET", "/api/v1/events", headers={"Last-Event-ID": "0"}) as stream:
            name, event_id, data = await next_event(stream.aiter_lines())
        assert data["job_id"] == saved["job_id"]
        if outcome == "done":
            assert name == "reply_committed" and data["reply"] == finished["reply"]
            # 初始化历史和补发事件可能重叠，按 message_id 合并后只有一条正式回复。
            merged = {item["message_id"]: item for item in history}
            merged[data["reply"]["message_id"]] = data["reply"]
            assert len(merged) == 2
        else:
            assert name == "generation_failed" and data["error"] == finished["error"]
            assert data["manual_retry_count"] == finished["manual_retry_count"]
        # 已消费游标之后没有重复业务事件，且重新打开不会重新调用模型。
        async with client.stream("GET", f"/api/v1/events?after={event_id}") as stream:
            lines = stream.aiter_lines()
            assert "retry" in await frame(lines)
            assert (await frame(lines)).get("heartbeat")
    assert len(test.model.gates) == 1


@pytest.mark.asyncio
async def test_reopen_while_generation_is_running(live_chat):
    test = live_chat
    saved, path, _ = await send_message(test.client, 1)
    assert await asyncio.wait_for(test.model.started.get(), 5) == 1
    await disconnect_subscription(test)
    async with reopened_client(test) as client:
        async with client.stream("GET", "/api/v1/events") as stream:
            lines = stream.aiter_lines()
            assert "retry" in await frame(lines)
            history, jobs = await snapshot(client, path)
            assert len(history) == len(jobs) == 1
            assert jobs[0]["job_id"] == saved["job_id"]
            assert jobs[0]["status"] == "need_generate"
            assert jobs[0]["manual_retry_count"] == 0
            test.model.gates[0].set()
            name, _, data = await next_event(lines)
            assert name == "reply_committed" and data["job_id"] == saved["job_id"]
            assert data["reply"]["content"] == ANSWER
        history, jobs = await snapshot(client, path)
        assert len(history) == 2 and jobs == []
    assert len(test.model.gates) == 1


@pytest.mark.asyncio
async def test_reopen_retry_with_old_failure_replayed(live_chat):
    test = live_chat
    test.model.failures.update((1, 2))
    saved, path, _ = await send_message(test.client, 1)
    assert await asyncio.wait_for(test.model.started.get(), 5) == 1
    test.model.gates[0].set()
    await wait_job(test.client, saved["job_id"], "failed")
    retry_path = f"/api/v1/generation-jobs/{saved['job_id']}/retry"
    response = await test.client.post(retry_path)
    assert response.status_code == 202 and response.json()["manual_retry_count"] == 1
    assert await asyncio.wait_for(test.model.started.get(), 5) == 2

    async with reopened_client(test) as client:
        async with client.stream("GET", "/api/v1/events") as stream:
            lines = stream.aiter_lines()
            history, jobs = await snapshot(client, path)
            assert len(history) == len(jobs) == 1
            current = jobs[0]
            assert current["status"] == "need_generate" and current["manual_retry_count"] == 1
            name, old_id, old = await next_event(lines)
            assert name == "generation_failed" and old["job_id"] == current["job_id"]
            assert old["manual_retry_count"] < current["manual_retry_count"]
            # 旧事件可由前端识别；读取旧事件不会修改后端任务。
            queried = await client.get(f"/api/v1/generation-jobs/{saved['job_id']}")
            assert queried.json() == current
            test.model.gates[1].set()
            name, failed_id, failed = await next_event(lines)
            assert name == "generation_failed" and failed_id > old_id
            _, jobs = await snapshot(client, path)
            assert jobs[0]["status"] == "failed"
            assert failed["manual_retry_count"] == jobs[0]["manual_retry_count"] == 1
            assert failed["error"] == jobs[0]["error"]
            response = await client.post(retry_path)
            assert response.status_code == 202 and response.json()["manual_retry_count"] == 2
            assert await asyncio.wait_for(test.model.started.get(), 5) == 3
            test.model.gates[2].set()
            name, done_id, done = await next_event(lines)
            assert name == "reply_committed" and done_id > failed_id
            assert done["job_id"] == saved["job_id"]
        history, jobs = await snapshot(client, path)
        assert len(history) == 2 and jobs == []
        assert (await wait_job(client, saved["job_id"], "done"))["manual_retry_count"] == 2
    assert len(test.model.gates) == 3 and len(test.model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("first_query", ["messages", "generation-jobs"])
async def test_generation_completes_between_initial_queries(live_chat, first_query):
    test = live_chat
    saved, path, _ = await send_message(test.client, 1)
    assert await asyncio.wait_for(test.model.started.get(), 5) == 1
    base = path.removesuffix("/messages")
    async with reopened_client(test) as client:
        # 先确认 SSE 已建立，再初始化页面；模型闸门精确控制两次查询之间完成。
        async with client.stream("GET", "/api/v1/events") as stream:
            lines = stream.aiter_lines()
            assert "retry" in await frame(lines)
            first = await client.get(f"{base}/{first_query}")
            assert first.status_code == 200 and len(first.json()["items"]) == 1
            test.model.gates[0].set()
            await wait_job(client, saved["job_id"], "done")
            second_query = "generation-jobs" if first_query == "messages" else "messages"
            second = await client.get(f"{base}/{second_query}")
            assert second.status_code == 200
            history = (first if first_query == "messages" else second).json()["items"]
            jobs = (first if first_query == "generation-jobs" else second).json()["items"]
            if first_query == "messages":
                # 两次查询本身存在空隙：旧历史没有回复，新任务列表也已没有待处理任务。
                assert len(history) == 1 and jobs == []
            else:
                assert len(history) == 2 and jobs[0]["status"] == "need_generate"
            name, event_id, data = await next_event(lines)
            assert name == "reply_committed" and data["job_id"] == saved["job_id"]
            merged = {item["message_id"]: item for item in history}
            merged[data["reply"]["message_id"]] = data["reply"]
            assert len(merged) == 2 and data["reply"]["content"] == ANSWER
        async with client.stream("GET", f"/api/v1/events?after={event_id - 1}") as stream:
            assert await next_event(stream.aiter_lines()) == (name, event_id, data)
        final_history, final_jobs = await snapshot(client, path)
        assert len(final_history) == 2 and final_jobs == []
    assert len(test.model.gates) == 1
