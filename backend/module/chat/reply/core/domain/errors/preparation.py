"""提示词资源与组装错误。"""
from module.shared.common.errors.base import InvalidStateError


class PromptResourceError(InvalidStateError):
    code = "prompt_resource_error"


class PromptMaterialError(InvalidStateError):
    code = "prompt_material_error"


class PromptBudgetExceeded(InvalidStateError):
    code = "prompt_budget_exceeded"
