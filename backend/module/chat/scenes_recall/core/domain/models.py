"""场景示例是表达参考，不是与用户共同经历的长期记忆。"""
from dataclasses import dataclass
from typing import Protocol


class SceneMessage(Protocol):
    """构建场景查询所需的对话片段。"""

    @property
    def role(self) -> str: ...

    @property
    def content(self) -> str: ...


@dataclass(frozen=True, slots=True)
class SceneExample:
    scene_id: str
    session_id: str
    score: float
    context: tuple[str, ...]
    reply: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class SceneQuery:
    character_id: str
    messages: tuple[SceneMessage, ...]
