"""召回输入和角色作用域不合法时的业务异常。

MemoryRecallTaskError 用于报告本次请求或材料与角色约束不一致。
工具可修正错误与公共资源不可用错误由 shared 的统一错误契约表达。
"""
from __future__ import annotations
from module.shared.common.errors.base import BusinessError


class MemoryRecallTaskError(BusinessError):
    code = "memory_recall_task_error"
