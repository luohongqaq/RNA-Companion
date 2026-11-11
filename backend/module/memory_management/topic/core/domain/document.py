"""Topic 正式主题正文及其组成值对象。

TopicDocument 组合主题身份、当前状态和统一时间线条目，表达事件进展、计划、证据及更正关系。
负责正文结构校验、序列化、内容哈希与公开投影；已加载正文的版本包装由 source 中的 TopicContext 表达。
"""
from __future__ import annotations
import hashlib
import json
from module.shared.memory.types.references import EvidenceRef
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from typing import Literal


TIMELINE_WINDOW_DAYS = 14


@dataclass(frozen=True, slots=True)
class TopicEntry:
    """统一时间线条目，显式区分已发生事件与未来计划。"""
    entry_id: str
    statement: str
    occurrence_time: date | None
    acquire_time: date
    source_refs: tuple[EvidenceRef, ...]
    corrects_entry_id: str | None = None
    kind: Literal["event", "plan"] = "event"
    actor: Literal["user", "assistant", "other"] = "user"
    target_time: date | None = None

    def __post_init__(self) -> None:
        # 用户内容允许无聊天来源；AI 新内容的来源要求由 TopicModelOutput 校验。
        if not self.entry_id or not self.statement.strip():
            raise ValueError("topic条目需要ID和陈述")
        if self.kind not in ("event", "plan") or self.actor not in ("user", "assistant", "other"):
            raise ValueError("topic条目类型或主体不合法")
        if (type(self.acquire_time) is not date
                or any(value is not None and type(value) is not date for value in (self.occurrence_time, self.target_time))):
            raise ValueError("topic时间线必须使用日期")
        if (self.kind == "plan" and self.occurrence_time is not None
                or self.kind == "event" and self.target_time is not None):
            raise ValueError("计划使用target_time，已发生事件使用occurrence_time")


@dataclass(frozen=True, slots=True)
class TopicIdentity:
    """同一事件主题的身份边界，供归属判断，不是已有events的摘要。"""
    description: str # 主题描述, 代表events的边界
    source_refs: tuple[EvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class TopicCurrentState:
    """一个topic中的最新的发展状态"""
    text: str
    source_entry_ids: tuple[str, ...]  # 只能引用有效event


@dataclass(frozen=True, slots=True)
class TopicTimelineView:
    document: TopicDocument
    visible_entries: tuple[TopicEntry, ...]


@dataclass(frozen=True, slots=True)
class TopicDocument:
    """topic记忆文档中的内容：主题、最新发展状态、历史事件记录"""
    topic: str
    topic_identity: TopicIdentity
    current_state: TopicCurrentState
    entries: tuple[TopicEntry, ...]

    def __post_init__(self) -> None:
        ids = {event.entry_id for event in self.entries}
        if not self.topic.strip() or len(ids) != len(self.entries):
            raise ValueError("topic名称不能为空，时间线条目ID不能重复")
        if not set(self.current_state.source_entry_ids) <= ids:
            raise ValueError("topic当前状态引用了不存在的时间线条目")
        if any(event.corrects_entry_id is not None and (
                event.corrects_entry_id not in ids or event.corrects_entry_id == event.entry_id) for event in self.entries):
            raise ValueError("topic更正必须指向保留的其他时间线条目")

    def to_data(self) -> dict:
        """结构化正文保留来源和日期；不执行存储层I/O。"""
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, default=lambda value: value.isoformat()))

    def timeline_for(self, window_date: date) -> TopicTimelineView:
        """清理过期计划，并选出本批展示给模型的时间线条目。"""
        expired = {
            entry.entry_id for entry in self.entries
            if entry.kind == "plan" and entry.target_time is not None
            and entry.target_time < window_date and entry.acquire_time < window_date
        }
        document = replace(
            self,
            entries=tuple(entry for entry in self.entries if entry.entry_id not in expired),
            current_state=replace(
                self.current_state,
                source_entry_ids=tuple(
                    entry_id for entry_id in self.current_state.source_entry_ids if entry_id not in expired
                ),
            ),
        )
        cutoff = window_date - timedelta(days=TIMELINE_WINDOW_DAYS)
        visible_entries = tuple(entry for entry in document.entries if entry.acquire_time >= cutoff)
        return TopicTimelineView(document, visible_entries)

    def content_hash(self) -> str:
        """与daily一致，hash覆盖完整结构化正文，包括来源引用。"""
        encoded = json.dumps(self.to_data(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def timeline_text(self, *, include_dates: bool = True) -> str:
        """沿用已保存的条目顺序；显示更正关系，但不暴露内部ID和证据。"""
        superseded = {item.corrects_entry_id for item in self.entries if item.corrects_entry_id is not None}
        roles = {"user": "用户", "assistant": "角色", "other": "其他主体"}
        lines = []
        for item in self.entries:
            status = "〔已被更正〕" if item.entry_id in superseded else ""
            if item.corrects_entry_id is not None:
                status += "〔更正〕"
            label = roles[item.actor] + ("计划" if item.kind == "plan" else "")
            body = f"{status}{label}：{item.statement}"
            if include_dates:
                body = f"{item.acquire_time.isoformat()} 得知｜{body}"
                if item.occurrence_time is not None:
                    body += f"（发生日期：{item.occurrence_time.isoformat()}）"
                if item.target_time is not None:
                    body += f"（目标日期：{item.target_time.isoformat()}）"
            lines.append("- " + body)
        return "\n".join(lines)

    def render_text(self) -> str:
        """正式用户正文无引用ID、证据quote、hash或审核批注。"""
        sections = [("事件主题说明", self.topic_identity.description),
                    ("当前状态", self.current_state.text),
                    ("时间线", self.timeline_text())]
        parts = [f"# {self.topic}"]
        parts.extend(f"## {title}\n\n{body}" for title, body in sections if body.strip())
        return "\n\n".join(parts).rstrip() + "\n"

    def public_payload(self) -> dict[str, str]:
        """完整公开分区正文；日期放正文中，引用只留在SQLite结构化记录。"""
        return {"title": self.topic, "topic_identity": self.topic_identity.description,
                "current_state": self.current_state.text, "entries": self.timeline_text()}
