"""召回模型边界错误目录与资源不可用异常。

ErrorSpec 统一错误码、默认文案和定位字段，供参数校验、准入、预算、工具执行与 finish 拒绝共用。
可修正错误以条目回填模型；内部账本不一致使用 ValueError，取消必须原样传播。
MemoryRecallUnavailable 表达模型、读取器或共享资源尚不可用的业务异常。
"""
from __future__ import annotations
from module.shared.common.errors.base import BusinessError
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorSpec:
    """一个错误信号：code 与默认 message；build() 生成回填用的错误条目。"""

    code: str
    message: str

    def build(self, message: str | None = None, **fields: Any) -> dict[str, Any]:
        """生成 {code, message, **定位字段}；动态文案覆盖默认值。"""
        return {"code": self.code, "message": message or self.message, **fields}


INVALID_TYPE = ErrorSpec("invalid_type", "类型不符合 Schema")


MISSING_FIELD = ErrorSpec("missing_field", "缺少必填字段")


EXTRA_FIELD = ErrorSpec("extra_field", "存在不允许的字段")


INVALID_LENGTH = ErrorSpec("invalid_length", "长度或数量超出允许范围")


BLANK_VALUE = ErrorSpec("blank_value", "字符串不能为空白")


DUPLICATE_ITEM = ErrorSpec("duplicate_item", "数组不允许重复项")


OUT_OF_RANGE = ErrorSpec("out_of_range", "数值超出允许范围")


INVALID_ENUM = ErrorSpec("invalid_enum", "值不在允许的枚举内")


INVALID_ARGUMENTS = ErrorSpec("invalid_arguments", "参数未通过语义校验")


UNKNOWN_TOOL = ErrorSpec("unknown_tool", "未知读取工具")


INVALID_SCOPE = ErrorSpec("invalid_scope", "范围入口不合法")


INAPPLICABLE_FIELD = ErrorSpec("inapplicable_field", "该模式下此字段应省略")


UNKNOWN_CARD_HANDLE = ErrorSpec("unknown_card_handle", "只能读取本轮已提供的卡片")


EVENT_TIME_UNSUPPORTED = ErrorSpec("event_time_unsupported", "请使用 conversation_time")


FALLBACK_REQUIRES_SEARCH = ErrorSpec("fallback_requires_search", "最后查找前须有本轮热池查找依据")


TOOL_BUDGET_EXHAUSTED = ErrorSpec("tool_budget_exhausted", "工具调用次数已耗尽")


DEADLINE_EXCEEDED = ErrorSpec("deadline_exceeded", "选择截止时间已到")


RESPONSE_BUDGET_REACHED = ErrorSpec("response_budget_reached", "剩余返回预算不足")


DUPLICATE_REQUEST = ErrorSpec("duplicate_request", "相同请求已完成，请复用结果或提供新线索")


FINAL_BUDGET_EXCEEDED = ErrorSpec("final_budget_exceeded", "最终材料超过文档数或 token 预算")


TOOL_EXECUTION_FAILED = ErrorSpec("tool_execution_failed", "工具执行失败")


INVALID_MATERIAL = ErrorSpec("invalid_material", "返回材料与账本不一致")


INCONSISTENT_SELECTION = ErrorSpec("inconsistent_selection", "结束状态与选择不一致")


MISSING_UNRESOLVED = ErrorSpec("missing_unresolved", "incomplete 必须说明缺口")


UNKNOWN_READ_HANDLE = ErrorSpec("unknown_read_handle", "正文不在本轮已读集合中")


SECTION_NOT_READ = ErrorSpec("section_not_read", "该句柄未覆盖分区")


DUPLICATE_SECTION = ErrorSpec("duplicate_section", "同一文档分区不得重复选择")


INVALID_JSON = ErrorSpec("invalid_json", "arguments 必须是可解析的 JSON 对象")


INVALID_TOOL_BATCH = ErrorSpec("invalid_tool_batch", "finish 须单独提交，且应遵守当前可用工具和预算")


__all__ = ["ErrorSpec"] + [name for name in globals() if name.isupper()]


class MemoryRecallUnavailable(BusinessError):
    code = "memory_recall_unavailable"
