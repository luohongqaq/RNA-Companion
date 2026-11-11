"""模型消息协议层：对模型响应的解析、校验与参数解码。

职责：只处理 OpenAI 工具调用协议的"形态"问题——消息结构、调用 id、
arguments 字符串解码。不做业务校验（结构校验
在 tools.validation，准入约束在 tools.registry 的 admission_checks）。
所有函数都是纯函数；输入是不可信的模型输出，结构不符时抛异常或返回
False，由主循环决定如何兜底。
"""
from __future__ import annotations
import json
from module.memory_recall.agent.core.domain.model import MemoryModelResult
from typing import Any


class SelectionToolProtocol:
    @staticmethod
    def object_dict(value: Any) -> dict[str, Any]:
        """统一模型消息的字典表示：dict 直接返回，Pydantic 消息按 JSON 模式序列化。"""
        if isinstance(value, dict):
            return value
        return value.model_dump(mode="json", exclude_none=True)

    @staticmethod
    def extract_message(response: MemoryModelResult) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """防御性取出 assistant 消息与工具调用列表；结构无效时抛异常由调用方接住。"""
        assistant_message = SelectionToolProtocol.object_dict(response.message)
        calls = assistant_message.get("tool_calls") or []
        if not isinstance(calls, list) or any(not isinstance(c, dict)
                                              or not isinstance(c.get("function"), dict) for c in calls):
            raise ValueError("无效工具调用结构")
        return assistant_message, calls

    @staticmethod
    def call_ids_valid(calls: list[dict[str, Any]]) -> bool:
        """每个调用必须有非空且互不重复的 id（tool 结果配对的协议依据）。"""
        return all(c.get("id") for c in calls) and len({c["id"] for c in calls}) == len(calls)

    @staticmethod
    def has_mixed_finish(calls: list[dict[str, Any]]) -> bool:
        """finish_selection 与其他工具同批出现：全批拒绝，保证 finish 单独提交。"""
        return len(calls) > 1 and any(c["function"].get("name") == "finish_selection" for c in calls)

    @staticmethod
    def parse_arguments(call: dict[str, Any]) -> dict[str, Any]:
        """把 arguments（JSON 字符串）解码为 dict；非字符串或非对象时抛 ValueError。

        call 的结构须先经 extract_message 校验（function 已保证为 dict）。
        """
        raw_arguments = call["function"].get("arguments", "{}")
        args = json.loads(raw_arguments) if isinstance(raw_arguments, str) else None
        if not isinstance(args, dict):
            raise ValueError("arguments 必须是 JSON 对象")
        return args
