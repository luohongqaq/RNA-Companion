"""角色提示词编辑所需的数据。"""
from dataclasses import dataclass
from typing import Literal

PromptType = Literal["character_card", "system_prompt", "tone_prompt"]


@dataclass(frozen=True, slots=True)
class CharacterPrompt:
    """一份提示词的正文和文件内容版本；版本用于检测并发修改。"""
    prompt_type: PromptType
    content: str
    version: str
