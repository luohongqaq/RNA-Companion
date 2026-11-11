"""记忆管理阶段失败的业务异常契约。

区分固定来源变化、模型不可用和索引不可用，模型及索引异常携带可重试标记。
供公共 workflow 选择重试、重新规划或终止策略，避免业务层依赖具体数据库或模型 SDK 的异常类型。
"""
from __future__ import annotations
from module.shared.common.errors.base import BusinessError, InvalidStateError


class MemorySourceChangedError(InvalidStateError):
    """固定原文、辅助材料或基础版本变化；应作废本次计划并重新规划。"""
    code = "memory_source_changed"


class MemoryModelUnavailable(BusinessError):
    """模型适配器提供可重试标记，不向业务层暴露具体SDK异常。"""
    code = "memory_model_unavailable"

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class MemoryIndexUnavailable(BusinessError):
    """向量存储失败，编码结果不缓存，重试整个索引阶段。"""
    code = "memory_index_unavailable"

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable
