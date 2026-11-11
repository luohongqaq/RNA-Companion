"""本次召回固定使用的 Skill 与总结提示词资源。

SelectionSkill 保存校验后的 Skill 名称、说明、指令与文件哈希；MemoryRecallResources 组合选择和总结资源。
资源内容与哈希由 repository/resources 加载，执行过程使用该固定资源快照。
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SelectionSkill:
    name: str
    description: str
    instructions: str
    file_hash: str


@dataclass(frozen=True, slots=True)
class MemoryRecallResources:
    selection_instructions: str
    skill_hash: str
    summary_prompt: str
    summary_prompt_hash: str
