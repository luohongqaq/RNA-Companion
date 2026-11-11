"""module.chat.reply.core.domain.config.model: 业务类型与配置，不执行外部I/O。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelInvocationSpec:
    """运行时模型调用配置，不随T2-B提示词材料持久化"""
    endpoint: str #模型端点标识
    model_name: str #模型名称
    call_params: dict[str, Any] #调用参数
    output_schema_version: str #输出约束版本
    timeout_seconds: float #超时秒数
