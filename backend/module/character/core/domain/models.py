"""角色领域类型；纯领域对象，不含外部 I/O。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from module.shared.character.document import CharacterCardDocument
from module.shared.context.types.visual import VisualState


@dataclass(frozen=True, slots=True)
class CharacterCard:
    """角色提示词正文及其来源目录，用于构造生成请求。"""
    character_id: str
    character_name: str
    content: str  # 完整内容
    path: Path #角色提示词目录，对应characters.card_path
    user_appellation: str = ""  # 角色对用户的称呼，来自 characters 表

    @property
    def document(self) -> CharacterCardDocument:
        return CharacterCardDocument(self.character_name, self.content, self.user_appellation)


@dataclass(frozen=True, slots=True)
class CharacterReply:
    """角色的中文回复和该回复的视觉状态。"""
    response_zh: str
    visual: VisualState
