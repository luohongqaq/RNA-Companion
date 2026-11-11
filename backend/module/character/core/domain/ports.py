"""角色业务所依赖的存储接口。"""
from typing import Protocol
from module.character.core.domain.resources import CharacterInfo, CharacterResourceManifest
from module.character.core.domain.prompts import CharacterPrompt, PromptType


class CharacterMetadataPort(Protocol):
    """查询角色名称和资源目录。"""
    async def get_character_source(self, character_id: str) -> CharacterInfo | None: ...
    async def list_characters(self) -> tuple[CharacterInfo, ...]: ...


class CharacterResourcePort(Protocol):
    """读取角色文件及按版本更新提示词。"""

    # 先用角色目录加载并校验清单，再依据登记的路径读取文件字节。
    async def load_character_resources(self, character: CharacterInfo) -> CharacterResourceManifest: ...
    async def read_character_file(self, character: CharacterInfo, file: str) -> bytes: ...
    async def get_character_prompt(self, character: CharacterInfo, prompt_type: PromptType) -> CharacterPrompt: ...
    async def save_character_prompt(self, character: CharacterInfo, prompt_type: PromptType,
                                    content: str, version: str) -> CharacterPrompt: ...
