"""按用途取得模型客户端，并复用相同的连接配置。"""
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass

from openai import AsyncOpenAI
from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class ModelConnection:
    api_key: SecretStr | None
    base_url: str | None


class ModelClientFactory:
    def __init__(self, connections: Mapping[str, ModelConnection]) -> None:
        self._connections = dict(connections)
        self._clients: dict[ModelConnection, AsyncOpenAI] = {}

    def get(self, purpose: str) -> AsyncOpenAI:
        connection = self._connections[purpose]
        client = self._clients.get(connection)
        if client is None:
            api_key = connection.api_key.get_secret_value() if connection.api_key else ""
            if not api_key:
                raise ValueError(f"模型用途缺少 API 密钥: {purpose}")
            client = AsyncOpenAI(api_key=api_key, base_url=connection.base_url, max_retries=0)
            self._clients[connection] = client
        return client

    async def close(self) -> None:
        async with AsyncExitStack() as cleanup:
            for client in self._clients.values():
                cleanup.push_async_callback(client.close)
            self._clients.clear()
