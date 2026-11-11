"""Agent 读取工具的注册说明。

ToolSpec 将工具名称、参数模型、读取会话方法、结果字段、调用上限和准入校验关联起来。
注册表与执行器使用同一说明，工具数据契约由 retrieval 定义，具体准入判断由 Agent 负责。
"""
from __future__ import annotations
from dataclasses import dataclass
from pydantic import BaseModel
from typing import Any, Callable


@dataclass(frozen=True)
class ToolSpec:
    """工具说明书；admission_checks(parsed, args, state) 返回错误列表（准入层：本轮状态、服务端政策、字段在场性）。"""

    name: str
    input_model: type[BaseModel]            # contracts 的输入模型，负责 Schema 之外的语义校验
    store_method: str                       # store 上的执行方法名
    result_key: str                         # 结果里材料列表的键："items" / "reads"
    max_calls: int                          # 本轮该工具的调用次数上限
    admission_checks: Callable[..., list[dict[str, Any]]]  # 准入校验
