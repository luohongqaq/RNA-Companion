"""一次记忆召回的输入与有效性约束。

MemoryRecallTask 固定 attempt、角色、工作上下文与角色卡，并携带输入是否仍有效的检查回调。
会话 ID 用于跨轮缓存作用域；该请求不直接持有数据库连接或模型客户端。
"""
from __future__ import annotations
from module.character.core.domain.models import CharacterCard
from module.shared.context.types.working import WorkingContext
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class MemoryRecallTask:
    """记忆召回skill输入：主要是当前上下文"""
    attempt_id: str
    character_id: str
    context: WorkingContext
    character_card: CharacterCard
    is_current: Callable[[], bool] | None = None
    conversation_id: str | None = None #跨轮缓存作用域；独立调试未提供时不共享缓存
