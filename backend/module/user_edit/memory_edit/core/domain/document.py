"""用户可查看和编辑的记忆正文结构。

定义 Daily 五类记忆栏目及 Topic 身份、状态和时间线的公开数据，并从正式文档显式投影。
公开结构保留用户编辑需要的分类、日期和条目关系，移除聊天来源、证据原话等内部信息。
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from pydantic import TypeAdapter, ValidationError
from module.shared.common.errors.base import InvalidInputError
from module.memory_management.shared.core.domain.document import DailyMemory, TopicMemory
from datetime import date
from module.memory_management.daily.core.domain.document import (
    DailyDocument,
    DailyCommitment,
    DailyIntention,
    DailyMoment,
    DailyProfileFact,
    DailyReflection,
    IntentionCertainty,
    IntentionOwner,
    IntentionStatus,
    MomentKind,
    ProfileFactKind,
)
from module.memory_management.topic.core.domain.document import TopicCurrentState, TopicDocument, TopicEntry, TopicIdentity
from typing import Literal


@dataclass(frozen=True, slots=True)
class DailyMomentContent:
    """日记中的值得记住的时刻；保留分类与正文，不返回聊天来源。"""
    kind: MomentKind
    text: str


@dataclass(frozen=True, slots=True)
class DailyProfileFactContent:
    """日记中的长期事实，例如背景、偏好、成就或关系。"""
    kind: ProfileFactKind
    text: str


@dataclass(frozen=True, slots=True)
class DailyIntentionContent:
    """愿望或计划；保留主体、确定程度及进展状态。"""
    owner: IntentionOwner
    certainty: IntentionCertainty
    status: IntentionStatus
    text: str
    target_date: date | None  # 目标日期未知时为 None


@dataclass(frozen=True, slots=True)
class DailyCommitmentContent:
    """已经明确确认的承诺，与愿望或计划分开展示。"""
    owner: IntentionOwner
    status: IntentionStatus
    text: str
    target_date: date | None  # 目标日期未知时为 None


@dataclass(frozen=True, slots=True)
class DailyReflectionContent:
    """角色在记录日记时形成的反思正文。"""
    text: str


@dataclass(frozen=True, slots=True)
class DailyDocumentContent:
    """供前端展示的日记正文，包含日期、标题和五个内容分区。"""
    memory_date: date  # 日记归属日期
    title: str
    memorable_moments: tuple[DailyMomentContent, ...] = ()
    profile_facts: tuple[DailyProfileFactContent, ...] = ()
    intentions: tuple[DailyIntentionContent, ...] = ()
    commitments: tuple[DailyCommitmentContent, ...] = ()
    reflections: tuple[DailyReflectionContent, ...] = ()

    @classmethod
    def from_document(cls, document: DailyDocument) -> DailyDocumentContent:
        """显式选取可展示字段；不复制来源session、turn ID和证据原话。"""
        return cls(
            memory_date=document.memory_date, title=document.title,
            memorable_moments=tuple(DailyMomentContent(item.kind, item.text) for item in document.memorable_moments),
            profile_facts=tuple(DailyProfileFactContent(item.kind, item.text) for item in document.profile_facts),
            intentions=tuple(DailyIntentionContent(item.owner, item.certainty, item.status, item.text, item.target_date)
                             for item in document.intentions),
            commitments=tuple(DailyCommitmentContent(item.owner, item.status, item.text, item.target_date)
                              for item in document.commitments),
            reflections=tuple(DailyReflectionContent(item.text) for item in document.reflections),
        )

    @classmethod
    def validate(cls, value: DailyDocumentContent) -> DailyDocumentContent:
        """先校验公开正文结构及非空条目，保留用户文本中的原始空白。"""
        try:
            content = TypeAdapter(cls).validate_python(asdict(value))
            texts = [content.title, *(item.text for field in (
                content.memorable_moments, content.profile_facts, content.intentions,
                content.commitments, content.reflections) for item in field)]
            if any(not text.strip() for text in texts):
                raise ValueError("标题和保留条目的正文不能为空")
        except (ValidationError, TypeError, ValueError) as exc:
            raise InvalidInputError("daily正文结构或字段无效") from exc
        return content

    def apply_to(self, memory: DailyMemory) -> DailyDocument:
        """固定归属日期和来源会话，仅为未改动的公开条目保留原证据。"""
        if self.memory_date != memory.memory_date:
            raise InvalidInputError("不能修改日记归属日期")
        # 相同公开条目保留旧证据；用户新增、改写的条目使用空来源。
        old = memory.document
        public = DailyDocumentContent.from_document(old)
        def preserve(field, items):
            originals = dict(zip(getattr(public, field), getattr(old, field), strict=True))
            return tuple(originals.get(item, new) for item, new in items)
        document = DailyDocument(self.memory_date, old.source_session_ids, self.title,
            preserve("memorable_moments", [(i, DailyMoment(i.kind, i.text, ())) for i in self.memorable_moments]),
            preserve("profile_facts", [(i, DailyProfileFact(i.kind, i.text, (), ())) for i in self.profile_facts]),
            preserve("intentions", [(i, DailyIntention(i.owner, i.certainty, i.status, i.text, i.target_date, (), ()))
                                    for i in self.intentions]),
            preserve("commitments", [(i, DailyCommitment(i.owner, i.status, i.text, i.target_date, ()))
                                     for i in self.commitments]),
            preserve("reflections", [(i, DailyReflection(i.text, ())) for i in self.reflections]))
        return document


@dataclass(frozen=True, slots=True)
class TopicIdentityContent:
    """主题说明，描述这件事的范围与身份边界。"""
    description: str


@dataclass(frozen=True, slots=True)
class TopicEntryContent:
    """主题时间线中的事件或计划；稳定ID用于状态引用和更正关系。"""
    entry_id: str  # 同一条目的稳定身份，更新文字时仍保留
    statement: str
    kind: Literal["event", "plan"]
    actor: Literal["user", "assistant", "other"]
    acquire_time: date  # 获知日期
    occurrence_time: date | None  # 事件发生日期；计划不使用这个字段
    target_time: date | None  # 计划目标日期；已发生事件不使用这个字段
    corrects_entry_id: str | None  # 更正条目指向被更正的旧条目；普通条目为 None


@dataclass(frozen=True, slots=True)
class TopicDocumentContent:
    """供前端展示的主题正文，包含主题说明、当前状态和时间线。"""
    topic: str
    topic_identity: TopicIdentityContent
    current_state: TopicCurrentState  # 复用不含聊天证据的状态类型；其中ID引用时间线条目
    entries: tuple[TopicEntryContent, ...] = ()

    @classmethod
    def from_document(cls, document: TopicDocument) -> TopicDocumentContent:
        """保留条目ID及关系，只移除内部来源；不改写正式正文。"""
        return cls(
            topic=document.topic,
            topic_identity=TopicIdentityContent(document.topic_identity.description),
            current_state=document.current_state,
            entries=tuple(TopicEntryContent(
                item.entry_id, item.statement, item.kind, item.actor, item.acquire_time,
                item.occurrence_time, item.target_time, item.corrects_entry_id,
            ) for item in document.entries),
        )

    @classmethod
    def validate(cls, value: TopicDocumentContent) -> tuple[TopicDocumentContent, TopicDocument]:
        """在读取旧版本前校验条目身份、状态引用和更正关系，构造无新增证据的正文。"""
        try:
            content = TypeAdapter(cls).validate_python(asdict(value))
            entries = tuple(TopicEntry(i.entry_id, i.statement, i.occurrence_time, i.acquire_time, (),
                i.corrects_entry_id, i.kind, i.actor, i.target_time) for i in content.entries)
            document = TopicDocument(content.topic, TopicIdentity(content.topic_identity.description, ()),
                                     content.current_state, entries)
        except (ValidationError, TypeError, ValueError) as exc:
            raise InvalidInputError("topic正文结构、条目ID或引用关系无效") from exc
        return content, document

    def apply_to(self, document: TopicDocument, memory: TopicMemory) -> TopicDocument:
        """将未改动的主题说明和时间线条目映射回原证据，新内容保留空来源。"""
        old = memory.document
        public = TopicDocumentContent.from_document(old)
        originals = dict(zip(public.entries, old.entries, strict=True))
        document = TopicDocument(document.topic,
            old.topic_identity if self.topic_identity == public.topic_identity else document.topic_identity,
            document.current_state, tuple(originals.get(item, new)
                for item, new in zip(self.entries, document.entries, strict=True)))
        return document
