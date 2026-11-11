"""回复请求和输出错误，供任务执行器分类处理。"""
from module.shared.common.errors.base import InvalidStateError


class ReplyModelUnavailable(InvalidStateError):
    code = "reply_model_unavailable"

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class ReplyOutputError(InvalidStateError):
    code = "reply_invalid_output"


class ReplyTruncatedError(ReplyOutputError):
    code = "reply_truncated"


class ReplyPromptMismatch(InvalidStateError):
    code = "reply_prompt_mismatch"
