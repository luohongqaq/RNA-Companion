"""Verify request options sent by the recall model adapter."""
from types import SimpleNamespace

import pytest

from module.memory_recall.agent.core.domain.model import MemoryModelRequest
from module.memory_recall.agent.repository.llm import CompatibleMemoryModelGateway


class FakeMessage:
    def __init__(self):
        self.content = "好"
        self.role = "assistant"
        self.tool_calls = None

    def model_dump(self, mode="json", exclude_none=False):
        return {"content": self.content, "role": self.role}


class RecordingClient:
    """记下每次收到的请求参数，方便断言网关怎么拼的参数。"""

    def __init__(self):
        self.calls = []

    async def _create(self, **params):
        self.calls.append(params)
        return SimpleNamespace(choices=[SimpleNamespace(message=FakeMessage(), finish_reason="stop")],
                               id="req_1", usage=None)

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))


def make_request(**overrides):
    defaults = dict(model="deepseek-chat", messages=[{"role": "user", "content": "你好"}],
                    timeout_seconds=5.0)
    return MemoryModelRequest(**{**defaults, **overrides})


@pytest.mark.asyncio
async def test_summary_request_keeps_thinking_disabled_shape_for_deepseek():
    client = RecordingClient()
    await CompatibleMemoryModelGateway(client).complete(make_request(thinking_enabled=False))
    params = client.calls[0]
    assert params["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in params


@pytest.mark.asyncio
async def test_always_thinking_model_gets_effort_instead_of_thinking_switch():
    client = RecordingClient()
    gateway = CompatibleMemoryModelGateway(client, send_thinking_parameter=False,
                                           always_thinking_effort="low")
    await gateway.complete(make_request(thinking_enabled=False))
    params = client.calls[0]
    assert params["reasoning_effort"] == "low"
    assert "extra_body" not in params and "tools" not in params
