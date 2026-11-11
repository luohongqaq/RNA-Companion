"""记忆召回 Agent 的模型调用能力接口。

MemoryModelPort 接收统一模型请求并返回标准化结果，由选择循环和总结流程共同使用。
具体模型 SDK、连接与调用参数转换封装在 agent/repository/llm.py。
"""
from __future__ import annotations
from module.memory_recall.agent.core.domain.model import MemoryModelRequest, MemoryModelResult
from typing import Protocol


class MemoryModelPort(Protocol):
    async def complete(self, request: MemoryModelRequest) -> MemoryModelResult: ...
