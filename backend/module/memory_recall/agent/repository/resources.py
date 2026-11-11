"""从模块prompts加载原始Skill和总结提示词，不读取外部项目或环境文件。"""
from __future__ import annotations
import hashlib
import logging
import re
import sys
import yaml
from module.memory_recall.agent.core.domain.resources import MemoryRecallResources, SelectionSkill
from pathlib import Path


PROMPTS = Path(__file__).resolve().parents[1] / "core" / "processing" / "prompts"


_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


_KNOWN_FIELDS = {"name", "description", "metadata", "license", "compatibility"}


class MemoryRecallResourceLoader:
    @staticmethod
    def load_skill(path: Path) -> SelectionSkill:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig").replace("\r\n", "\n")
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise ValueError("missing skill frontmatter")
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            raise ValueError("unclosed skill frontmatter")
        meta = yaml.safe_load("".join(lines[1:end]))
        if not isinstance(meta, dict):
            raise ValueError("invalid skill metadata")

        for field in sorted(set(meta) - _KNOWN_FIELDS):
            logging.getLogger(__name__).warning("skill_loader：未知顶层字段 %r", field)

        name = meta.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or not _NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid name: {name!r}")
        if name != path.parent.name:
            raise ValueError(f"name {name!r} 与目录名 {path.parent.name!r} 不一致")

        description = meta.get("description")
        if not isinstance(description, str) or not 1 <= len(description) <= 1024:
            raise ValueError("description 必须是 1~1024 字符")

        metadata = meta.get("metadata", {})
        if not isinstance(metadata, dict) or any(not isinstance(value, str) for value in metadata.values()):
            raise ValueError("metadata 必须是 string -> string（version 记得加引号）")

        instructions = "".join(lines[end + 1:]).strip()
        if not instructions:
            raise ValueError("empty skill instructions")
        if instructions.startswith("---"):
            raise ValueError("疑似残留第二个 frontmatter 块（粘贴事故）")

        return SelectionSkill(
            name=name,
            description=description,
            instructions=instructions,
            file_hash=hashlib.sha256(raw).hexdigest(),
        )

    @staticmethod
    def load(root: Path = PROMPTS) -> MemoryRecallResources:
        skill = MemoryRecallResourceLoader.load_skill(root / "memory-selection" / "SKILL.md")
        summary = (root / "memory-summary.md").read_bytes()
        text = summary.decode("utf-8-sig").replace("\r\n", "\n")
        if not text.strip():
            raise ValueError("记忆总结提示词为空")
        return MemoryRecallResources(skill.instructions, skill.file_hash, text,
                                     hashlib.sha256(summary).hexdigest())
