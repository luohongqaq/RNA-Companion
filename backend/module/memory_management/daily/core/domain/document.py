"""Daily 正式记忆正文及组成正文的值对象。

定义重要时刻、人物事实、意图、承诺、反思及其分类枚举，由 DailyDocument 组合完整日记忆。
负责正文解析、来源轮次校验、内容哈希和文本投影，为生成校验、持久化与索引提供统一正文格式。
"""
from __future__ import annotations
import hashlib
import json
from module.shared.common.errors.base import InvalidStateError
from dataclasses import asdict, dataclass, replace
from datetime import date
from enum import StrEnum


class MomentKind(StrEnum):
    MILESTONE = "milestone"
    EMOTIONAL_ARC = "emotional_arc"
    RELATIONSHIP = "relationship"


class ProfileFactKind(StrEnum):
    BACKGROUND = "background"
    PREFERENCE = "preference"
    ACHIEVEMENT = "achievement"
    RELATIONSHIP = "relationship"
    BOUNDARY = "boundary"


class IntentionOwner(StrEnum):
    USER = "user"
    CHARACTER = "character"
    BOTH = "both"


class IntentionCertainty(StrEnum):
    WISH = "wish"
    PLAN = "plan"


class IntentionStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DailyMoment:
    """角色第一人称记录的重要经历；不作为自动更新用户画像的事实。"""
    kind: MomentKind
    text: str
    source_turn_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DailyProfileFact:
    """有证据的画像更新材料；稳定偏好与长期变化，尚未自动合并进正式画像。"""
    kind: ProfileFactKind
    text: str
    source_turn_ids: tuple[str, ...]
    evidence_quotes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DailyIntention:
    """概括未完事项，原话单独作为内部证据；期限未知时不补造。"""
    owner: IntentionOwner
    certainty: IntentionCertainty
    status: IntentionStatus
    text: str
    target_date: date | None
    source_turn_ids: tuple[str, ...]
    evidence_quotes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DailyReflection:
    """写日记时角色形成的看法与反思，不是过去说过的话或用户事实。"""
    text: str
    source_turn_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DailyCommitment:
    """相应主体明确确认的承诺，与愿望、打算分开。"""
    owner: IntentionOwner
    status: IntentionStatus
    text: str
    target_date: date | None
    source_turn_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DailyDocument:
    memory_date: date
    source_session_ids: tuple[str, ...]
    title: str
    memorable_moments: tuple[DailyMoment, ...] = ()
    profile_facts: tuple[DailyProfileFact, ...] = ()
    intentions: tuple[DailyIntention, ...] = ()
    commitments: tuple[DailyCommitment, ...] = ()
    reflections: tuple[DailyReflection, ...] = ()

    @property
    def has_memories(self) -> bool:
        return bool(self.memorable_moments or self.profile_facts or self.intentions or self.commitments or self.reflections)

    def to_data(self) -> dict:
        """模型输入与SQLite共用完整正文协议，不输出内部Python类型。"""
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, default=lambda value: value.isoformat()))

    @classmethod
    def from_data(cls, data: dict) -> DailyDocument:
        """原样读取持久化正文，保留用户空白；生成输出的文本清理仍在模型入口。"""
        return cls._parse(data, memory_date=date.fromisoformat(data["memory_date"]),
                          source_session_ids=tuple(data["source_session_ids"]), allowed_turn_ids=None,
                          require_quotes=False, preserve_text=True)

    @classmethod
    def from_model_data(cls, data: dict, *, session_id: str, memory_date: date,
                        source_session_ids: tuple[str, ...], allowed_turn_ids: set[str]) -> DailyDocument:
        fields = {"session_id", "title", "memorable_moments", "profile_facts", "intentions", "commitments", "reflections"}
        if not isinstance(data, dict) or set(data) != fields or data["session_id"] != session_id:
            raise InvalidStateError("daily输出字段或session_id不符合约定")
        return cls._parse(data, memory_date=memory_date, source_session_ids=source_session_ids,
                          allowed_turn_ids=allowed_turn_ids, require_quotes=True)

    @classmethod
    def _parse(cls, data: dict, *, memory_date: date, source_session_ids: tuple[str, ...],
               allowed_turn_ids: set[str] | None, require_quotes: bool, preserve_text: bool = False) -> DailyDocument:
        moments = tuple(DailyMoment(cls._enum(MomentKind, item["kind"]), cls._text(item["text"], preserve=preserve_text),
            cls._refs(item["source_turn_ids"], allowed_turn_ids)) for item in
            cls._rows(data, "memorable_moments", {"kind", "text", "source_turn_ids"}))
        facts = []
        for item in cls._rows(data, "profile_facts", {"kind", "text", "source_turn_ids", "evidence_quotes"}):
            quotes = cls._quotes(item["evidence_quotes"], required=require_quotes)
            facts.append(DailyProfileFact(cls._enum(ProfileFactKind, item["kind"]), cls._text(item["text"], preserve=preserve_text),
                cls._refs(item["source_turn_ids"], allowed_turn_ids), tuple(quotes)))
        intentions = []
        for item in cls._rows(data, "intentions", {"owner", "certainty", "status", "text", "target_date", "source_turn_ids", "evidence_quotes"}):
            try:
                target = date.fromisoformat(item["target_date"]) if item["target_date"] is not None else None
            except (ValueError, TypeError) as error:
                raise InvalidStateError("daily事项日期必须为ISO日期或null") from error
            intentions.append(DailyIntention(cls._enum(IntentionOwner, item["owner"]),
                cls._enum(IntentionCertainty, item["certainty"]), cls._enum(IntentionStatus, item["status"]),
                cls._text(item["text"], preserve=preserve_text), target, cls._refs(item["source_turn_ids"], allowed_turn_ids),
                cls._quotes(item["evidence_quotes"], required=require_quotes)))
        commitments = []
        for item in cls._rows(data, "commitments", {"owner", "status", "text", "target_date", "source_turn_ids"}):
            try:
                target = date.fromisoformat(item["target_date"]) if item["target_date"] is not None else None
            except (ValueError, TypeError) as error:
                raise InvalidStateError("daily承诺日期必须为ISO日期或null") from error
            commitments.append(DailyCommitment(cls._enum(IntentionOwner, item["owner"]),
                cls._enum(IntentionStatus, item["status"]), cls._text(item["text"], preserve=preserve_text), target,
                cls._refs(item["source_turn_ids"], allowed_turn_ids)))
        reflections = tuple(DailyReflection(cls._text(item["text"], preserve=preserve_text),
            cls._refs(item["source_turn_ids"], allowed_turn_ids)) for item in
            cls._rows(data, "reflections", {"text", "source_turn_ids"}))
        return cls(memory_date, source_session_ids, cls._text(data["title"], preserve=preserve_text), moments, tuple(facts),
                   tuple(intentions), tuple(commitments), reflections)

    @staticmethod
    def _quotes(value: object, *, required: bool) -> tuple[str, ...]:
        """用户内容允许无证据；AI 生成仍要求非空原话，元素类型始终校验。"""
        if (not isinstance(value, list) or (required and not value)
                or any(not isinstance(q, str) or not q.strip() for q in value)):
            raise InvalidStateError("daily证据必须附非空原话摘录")
        return tuple(value)

    @staticmethod
    def _rows(data: dict, field: str, keys: set[str]) -> list[dict]:
        value = data[field]
        if not isinstance(value, list) or any(not isinstance(item, dict) or set(item) != keys for item in value):
            raise InvalidStateError(f"daily的{field}数组结构不符合约定")
        return value

    @staticmethod
    def _text(value: object, *, preserve: bool = False) -> str:
        if not isinstance(value, str) or not value.strip():
            raise InvalidStateError("daily条目必须为非空文本")
        return value if preserve else value.strip()

    @staticmethod
    def _enum(kind, value):
        try:
            return kind(value)
        except (ValueError, TypeError) as error:
            raise InvalidStateError("daily分类、主体、确定程度或状态不合法") from error

    @staticmethod
    def _refs(value: object, allowed: set[str] | None) -> tuple[str, ...]:
        if (not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value)
                or (allowed is not None and (not value or any(item not in allowed for item in value)))
                or len(value) != len(set(value))):
            raise InvalidStateError("daily来源引用缺失、重复或超出允许范围")
        return tuple(value)

    def collect_source_turn_ids(self) -> set[str]:
        return {turn_id for item in (*self.memorable_moments, *self.profile_facts, *self.intentions,
                                     *self.commitments, *self.reflections) for turn_id in item.source_turn_ids}

    def content_hash(self) -> str:
        encoded = json.dumps(self.to_data(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def public_payload(self) -> dict[str, str]:
        """可筛选的五个分区正文；不携带内部引用、证据或审核材料。"""
        fields = ("memorable_moments", "profile_facts", "intentions", "commitments", "reflections")
        result = {"title": self.title, "memory_date": self.memory_date.isoformat()}
        prefix = f"# {self.title}\n\n日期：{self.memory_date}"
        for field in fields:
            section = replace(self, **{key: getattr(self, key) if key == field else () for key in fields})
            result[field] = section.render_text().removeprefix(prefix).strip()
        return result

    def render_text(self, *, include_evidence: bool = False) -> str:
        """生成供索引和模型读取的文本；仅显式请求时携带内部证据。"""
        lines = [f"# {self.title}", "", f"日期：{self.memory_date}"]
        groups = (("值得记住的时刻", self.memorable_moments), ("长期事实", self.profile_facts),
                  ("想做与打算", self.intentions), ("明确承诺", self.commitments),
                  ("我的反思", self.reflections))
        for title, items in groups:
            if not items:
                continue
            lines += ["", "## " + title, ""]
            for item in items:
                text = item.text
                if isinstance(item, (DailyIntention, DailyCommitment)):
                    owner = {IntentionOwner.USER: "用户", IntentionOwner.CHARACTER: "我", IntentionOwner.BOTH: "双方"}[item.owner]
                    status = {IntentionStatus.OPEN: "待继续", IntentionStatus.COMPLETED: "已完成", IntentionStatus.CANCELLED: "已取消"}[item.status]
                    certainty = ({IntentionCertainty.WISH: "愿望", IntentionCertainty.PLAN: "打算"}[item.certainty]
                                 if isinstance(item, DailyIntention) else "承诺")
                    text += (f"（{status}" if isinstance(item, DailyIntention) else f"（{owner}的{certainty}，{status}")
                    text += f"，日期：{item.target_date}）" if item.target_date is not None else "）"
                lines += [text, ""]
                if include_evidence:
                    lines += ["来源：" + ", ".join(item.source_turn_ids), ""]
                    if isinstance(item, (DailyProfileFact, DailyIntention)):
                        lines += ["证据原话：" + "；".join(item.evidence_quotes), ""]
        return "\n".join(lines).rstrip() + "\n"
