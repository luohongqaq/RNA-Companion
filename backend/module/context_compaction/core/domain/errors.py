"""压缩阶段的来源失效、模型调用失败和候选拒绝异常。

调度根据异常类别与可重试标记决定重新规划、退避或终止任务。
"""
from module.shared.common.errors.base import BusinessError, InvalidStateError


class ContextSourceChangedError(InvalidStateError):
    """固定来源已缺失、失效或内容发生变化，不能继续使用当前尝试。"""
    code = "context_source_changed"


class ContextModelUnavailable(BusinessError):
    """摘要模型调用失败；SDK细节不进入worker的业务判断。"""
    code = "context_model_unavailable"

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ContextGenerationRejected(ContextModelUnavailable):
    """模型输出不合格；retryable由压缩重生成额度决定。"""
    code = "context_generation_rejected"
