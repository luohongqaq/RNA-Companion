"""context.summary: cohesive domain types; no external I/O."""
from __future__ import annotations

from module.shared.context.types.references import ContextSourceRef
from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Literal
import hashlib
import json

from pydantic import TypeAdapter

from module.shared.common.errors.base import InvalidStateError


class CommitmentStatus(StrEnum):
    ACTIVE = "active"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class StateChangeKind(StrEnum):
    CORRECTION = "correction"
    RETRACTION = "retraction"
    COMMITMENT_UPDATE = "commitment_update"


@dataclass(frozen=True, slots=True)
class Commitment:
    """已确认决定、偏好、边界或承诺；终止状态保留但退出活跃prompt。"""
    commitment_id: str
    role: Literal["user", "assistant", "both"]
    text: str
    revision: int
    status: CommitmentStatus
    source_refs: tuple[ContextSourceRef, ...]
    decided_on: date | None = None #作出决定、确认偏好或承诺的日期
    planned_for: date | None = None #明确的执行日期；没有具体日期则为空

    def __post_init__(self) -> None:
        for name in ("decided_on", "planned_for"):
            value = getattr(self, name)
            if value is not None and type(value) is not date:
                raise ValueError(f"{name}必须为日期或None")


@dataclass(frozen=True, slots=True)
class ObjectBinding:
    """实体绑定；解释称谓、角色或对象。"""
    reference: str #需要解释的称呼或指代
    object: str #来源支持的对象，不确定时明确表达不确定性
    source_refs: tuple[ContextSourceRef, ...]


@dataclass(frozen=True, slots=True)
class ToneContext:
    """语气、角色或场景状态,这里的role指的就是user"""
    emotion: str
    temporal_context: str | None #日期或相对时间归属，未知时不编造
    current_status: str | None #是否缓和、已结束或仍不确定
    source_refs: tuple[ContextSourceRef, ...]


@dataclass(frozen=True, slots=True)
class UnresolvedQuestion:
    """未解决的问题"""
    text: str
    source_refs: tuple[ContextSourceRef, ...]


@dataclass(frozen=True, slots=True)
class LatestTopic:
    """最新话题"""
    text: str
    source_refs: tuple[ContextSourceRef, ...]


@dataclass(frozen=True, slots=True)
class SceneState:
    """仅保留原文支持的剧情状态；日常聊天可以完全不提供。"""
    location: str | None = None
    story_time: str | None = None
    participants: tuple[str, ...] = ()
    ongoing_actions: tuple[str, ...] = ()
    relationship_context: tuple[str, ...] = ()
    character_known_information: tuple[str, ...] = ()
    character_unknown_information: tuple[str, ...] = ()
    source_refs: tuple[ContextSourceRef, ...] = ()


@dataclass(frozen=True, slots=True)
class StructuredSummary:
    """摘要正文；空分区不渲染，最近原文优先于边界处状态。"""
    summary: str #历史摘要，标明现实/剧情/设定/假设以及时间和情绪归属
    commitments: tuple[Commitment, ...] = ()
    active_thread: str | None = None
    object_bindings: tuple[ObjectBinding, ...] = ()
    tone_context: tuple[ToneContext, ...] = ()
    unresolved_questions: tuple[UnresolvedQuestion, ...] = ()
    latest_topic: LatestTopic | None = None
    source_refs: tuple[ContextSourceRef, ...] = ()
    scene_state: SceneState | None = None
    keywords: tuple[str, ...] = () #渲染给回复模型看的本session话题关键词

    def __post_init__(self) -> None:
        if (not isinstance(self.keywords, tuple)
                or any(not isinstance(word, str) or not word.strip() or word != word.strip() for word in self.keywords)
                or len({word.casefold() for word in self.keywords}) != len(self.keywords)):
            raise ValueError("keywords必须为去重、非空且无首尾空白的关键词元组")

    @classmethod
    def from_model_data(cls, value: Any) -> StructuredSummary:
        """外部模型JSON在输入边界校验，再转为既有数据类。"""
        try:
            if not isinstance(value, dict) or not isinstance(value.get("summary"), str):
                raise ValueError("缺少summary文本")
            unknown_fields = set(value) - set(cls.__dataclass_fields__)
            if unknown_fields:
                raise ValueError("包含未知摘要字段：" + ", ".join(sorted(unknown_fields)))
            # 模型可能用null表示空分区；仅将无内容的可选数组归一为空数组，不修补事实和引用。
            value = dict(value)
            for name in ("commitments", "object_bindings", "tone_context", "unresolved_questions"):
                if value.get(name) is None:
                    value[name] = []
            summary = TypeAdapter(cls).validate_json(json.dumps(value, ensure_ascii=False), strict=True)
            if not summary.keywords:
                raise ValueError("缺少本session的话题关键词")
            if any(item.decided_on is None for item in summary.commitments):
                raise ValueError("有效约定缺少决定日期decided_on")
            if any(item.current_status in ("未知或null", "null", "None") for item in summary.tone_context):
                raise ValueError("无法判断的状态必须使用JSON null")
            if not summary.summary.strip() or not summary.source_refs:
                raise ValueError("摘要正文和来源不能为空")
            for item in (*summary.commitments, *summary.object_bindings, *summary.tone_context,
                         *summary.unresolved_questions, *(() if summary.latest_topic is None else (summary.latest_topic,)),
                         *(() if summary.scene_state is None else (summary.scene_state,))):
                if not item.source_refs:
                    raise ValueError("非空摘要分区必须提供来源")
            for ref in summary.collect_source_refs():
                if not ref.turn_id.strip() or not ref.session_id or type(ref.revision) is not int or ref.revision < 1:
                    raise ValueError("来源引用不合法")
            return summary
        except (ValueError, TypeError) as error:
            raise InvalidStateError(f"模型摘要结构或来源引用不合法：{error}") from error

    def collect_source_refs(self) -> set[ContextSourceRef]:
        """收集正文及所有非空分区的来源引用。"""
        refs = set(self.source_refs)
        for item in (*self.commitments, *self.object_bindings, *self.tone_context,
                     *self.unresolved_questions):
            refs.update(item.source_refs)
        if self.latest_topic is not None:
            refs.update(self.latest_topic.source_refs)
        if self.scene_state is not None:
            refs.update(self.scene_state.source_refs)
        return refs

    def validation_issues(self) -> tuple[str, ...]:
        """检查摘要自身的交接字段，来源范围与token预算由业务流程核对。"""
        issues = []
        if not self.keywords:
            issues.append("摘要缺少本session的话题关键词")
        for item in self.commitments:
            if item.decided_on is None:
                issues.append(f"约定{item.commitment_id}缺少决定日期")
            if not item.source_refs:
                issues.append(f"约定{item.commitment_id}缺少来源")
        return tuple(issues)

    def content_hash(self) -> str:
        """完整结构的稳定hash，包括关键词和来源元数据。"""
        encoded = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def render(self) -> str:
        """显式渲染参与回复的分区，不将来源ID等审计字段塞进模型上下文。"""
        sections = {
            "历史": self.summary,
            "有效约定": [{"主体": item.role, "内容": item.text,
                          "决定日期": item.decided_on.isoformat() if item.decided_on is not None else None,
                          "执行日期": item.planned_for.isoformat() if item.planned_for is not None else None}
                         for item in self.commitments
                         if item.status == CommitmentStatus.ACTIVE],
            "当前对话主线": self.active_thread,
            "指代关系": [{"称呼": item.reference, "含义": item.object} for item in self.object_bindings],
            "语气背景": [{"情绪": item.emotion, "时间": item.temporal_context, "状态": item.current_status}
                         for item in self.tone_context],
            "未完成事项": [item.text for item in self.unresolved_questions],
            "最新话题": self.latest_topic.text if self.latest_topic is not None else None,
            "聊天关键词": list(self.keywords),
        }
        scene = self.scene_state
        if scene is not None:
            sections["剧情状态"] = {
                "地点": scene.location, "时间": scene.story_time, "参与者": scene.participants,
                "进行中动作": scene.ongoing_actions, "关系": scene.relationship_context,
                "角色已知": scene.character_known_information, "角色未知": scene.character_unknown_information,
            }
        return json.dumps({key: value for key, value in sections.items() if value},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class SummaryVersions:
    """摘要结构与生成规则的版本信息，不表示同session存在多份摘要。"""
    version: int #结构协议版本
    prompt_version: str = "unconfigured"
    model_name: str = "unconfigured"
    tokenizer_version: str = "unconfigured"
