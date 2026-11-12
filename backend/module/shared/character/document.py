"""角色卡正文及其面向模型的段落视图。"""
import hashlib
import re
from dataclasses import dataclass

SELECTION_SECTIONS = ("身份", "核心品格", "关系立场", "人格使用边界", "注意力与认知", "日常陪伴基调")


@dataclass(frozen=True, slots=True)
class CharacterSelectionView:
    name: str
    rules: str
    source_hash: str
    user_appellation: str = ""  # 角色对用户的称呼，记忆总结用它替换 {{ user_name }}

    def selection_context(self) -> dict[str, str]:
        return {"character_name": self.name, "rules": self.rules, "source_hash": self.source_hash,
                "user_appellation": self.user_appellation}


@dataclass(frozen=True, slots=True)
class CharacterCardDocument:
    """从现有角色卡正文提取视图。"""

    character_name: str
    content: str
    user_appellation: str = ""

    def selection_view(self) -> CharacterSelectionView:
        text = self.content.replace("\r\n", "\n")
        rules = "\n\n".join(self._section(text, title) for title in SELECTION_SECTIONS)
        return CharacterSelectionView(self.character_name, rules,
                                      hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                      self.user_appellation)

    @staticmethod
    def validate_selection_sections(text: str) -> None:
        """校验必需标题恰好各出现一次；缺失或重复时抛 ValueError。

        供角色登记在启动阶段调用：记忆召回的选段视图依赖这六个标题，
        缺失时每轮生成都会失败，必须在登记时就暴露，而不是等到发消息。
        """
        headings = CharacterCardDocument._scan_headings(text.replace("\r\n", "\n"))
        names = [name for _, _, name in headings]
        problems = []
        for title in SELECTION_SECTIONS:
            count = names.count(title)
            if count == 0:
                problems.append(f"缺少“{title}”")
            elif count > 1:
                problems.append(f"“{title}”出现{count}次")
        if problems:
            raise ValueError("角色卡" + "；".join(problems))

    @staticmethod
    def _scan_headings(text: str) -> list[tuple[int, int, str]]:
        """扫描标题行（忽略代码块中的行），返回 (行号, 层级, 标题)。"""
        lines = text.splitlines(keepends=True)
        headings = []
        fence = None
        for index, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith(('```', '~~~')):
                marker = stripped[:3]
                if fence is None:
                    fence = marker
                elif marker == fence:
                    fence = None
                continue
            if fence:
                continue
            match = re.match(r'^(#{1,6})\s+(.+?)\s*$', line)
            if match:
                headings.append((index, len(match[1]), match[2]))
        return headings

    @staticmethod
    def _section(text: str, title: str) -> str:
        """提取指定标题到下一同级/更高标题；忽略代码块中的标题。"""
        lines = text.splitlines(keepends=True)
        headings = CharacterCardDocument._scan_headings(text)
        found = [(i, level) for i, level, name in headings if name == title]
        if len(found) != 1:
            raise ValueError(f'角色卡必须恰好包含一个“{title}”标题，实际为{len(found)}个')
        start, level = found[0]
        end = next((i for i, other, _ in headings if i > start and other <= level), len(lines))
        return ''.join(lines[start:end]).strip()
