"""记忆索引的表示分类、人物引用和候选结果。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RepresentationKind(StrEnum):
    """topic文档的的内容"""
    DAILY_MEMORY = "daily_memory"  # 向量数据库设计小节采用一条主要向量
    TOPIC_NAME = "topic_name"
    TOPIC_IDENTITY = "topic_identity"
    TOPIC_CURRENT = "topic_current"
    TOPIC_TIMELINE = "topic_timeline"


@dataclass(frozen=True, slots=True)
class PersonRef:
    name: str
    person_id: str | None = None


@dataclass(frozen=True, slots=True)
class IndexHit:
    """正文候选；point身份和分数在返回值外层，正文使用前仍需核对SQLite。"""
    point_id: str
    character_id: str
    score: float
    title: str
    memory_date: str
    sections: dict[str, str]

    @property
    def full_content(self) -> str:
        body = "\n\n".join(value for value in self.sections.values() if value)
        return f"# {self.title}\n\n日期：{self.memory_date}\n\n{body}".rstrip() + "\n"
