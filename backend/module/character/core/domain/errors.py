"""角色资源不可用时的业务错误。"""
from module.shared.common.errors.base import InvalidStateError


class CharacterResourceError(InvalidStateError):
    """角色资源缺失或配置无效。"""
