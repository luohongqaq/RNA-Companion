"""类型化查询结果及 HTTP 序列化契约。"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from api.app import create_app
from api.dependencies import get_generation_query_service
from module.shared.context.types.visual import Expression, Pose, VisualState
from module.chat.conversation.core.domain.contracts import AcceptedReply
from module.chat.reply.core.domain.models import (
    GenerationFailure, GenerationFailureKind, GenerationJob, GenerationJobStatus,
)
from module.chat.reply.core.processing.query import GenerationQueryService
from module.chat.reply.core.domain.query import GenerationJobResult


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    GenerationJobStatus.NEED_GENERATE, GenerationJobStatus.FAILED, GenerationJobStatus.DONE,
])
async def test_query_returns_typed_result_and_serializes_nested_fields(status):
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    job = GenerationJob.register_generate_reply("job", "turn", 2, now)
    job.status = status
    job.manual_retry_count = 2
    if status == GenerationJobStatus.FAILED:
        job.last_error = GenerationFailure(GenerationFailureKind.NETWORK, "timeout", "超时", True, now)
        job.retry_count = 3
    reply = AcceptedReply("conv", "turn", "message", "你好", VisualState(
        Pose("pose", "站立"), Expression("expression", "微笑")), now)
    messages = SimpleNamespace(reply_message=AsyncMock(return_value=reply))
    service = GenerationQueryService(SimpleNamespace(get_job=AsyncMock(return_value=job)), messages)
    result = await service.get("job")
    assert isinstance(result, GenerationJobResult)
    assert result.manual_retry_count == 2
    assert result.status is status
    assert result.error is job.last_error
    assert result.reply == (reply if status == GenerationJobStatus.DONE else None)
    if status != GenerationJobStatus.DONE:
        messages.reply_message.assert_not_awaited()
    app = create_app()
    app.dependency_overrides[get_generation_query_service] = lambda: service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/generation-jobs/job")
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"job_id", "turn_id", "input_version", "status", "retry_count",
                         "available_at", "error", "reply", "manual_retry_count"}
    assert data["status"] == status.value
    assert data["input_version"] == 2
    assert data["manual_retry_count"] == 2
    if status == GenerationJobStatus.DONE:
        assert data["reply"]["content"] == "你好"
        assert data["reply"]["visual"]["pose_name"]["pose_name"] == "站立"
        assert data["reply"]["created_at"].startswith("2026-11-03")
    else:
        assert data["reply"] is None
    if status == GenerationJobStatus.FAILED:
        assert data["error"]["kind"] == "network"
        assert data["error"]["occurred_at"].startswith("2026-11-03")
        assert data["retry_count"] == 3
    else:
        assert data["error"] is None
    schema = app.openapi()
    assert schema["paths"]["/api/v1/generation-jobs/{job_id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/GenerationJobResponse")


@pytest.mark.asyncio
async def test_query_missing_job_is_404():
    messages = SimpleNamespace(reply_message=AsyncMock())
    service = GenerationQueryService(SimpleNamespace(get_job=AsyncMock(return_value=None)), messages)
    app = create_app()
    app.dependency_overrides[get_generation_query_service] = lambda: service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/generation-jobs/missing")
    assert response.status_code == 404
    messages.reply_message.assert_not_awaited()
