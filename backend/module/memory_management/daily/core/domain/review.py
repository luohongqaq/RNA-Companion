"""Daily 审核问题与绑定候选的审核结论。

定义问题类别、问题明细和 DailyReview，校验通过、修订通过、拒绝三种结论与修订正文的对应关系。
负责解析审核输出并形成可用于重提取的反馈，供任务校验当前候选及选择最终发布正文。
"""
from __future__ import annotations
from module.shared.common.errors.base import InvalidStateError
from dataclasses import dataclass
from enum import StrEnum
from module.memory_management.daily.core.domain.document import DailyDocument
from module.memory_management.shared.contracts.review import ReviewSeverity, ReviewVerdict
from typing import Literal


class DailyIssueType(StrEnum):
    """daily记忆问题类型"""
    UNSUPPORTED_CLAIM = "unsupported_claim"
    WRONG_ATTRIBUTION = "wrong_attribution"
    INCORRECT_FACT = "incorrect_fact"
    MISSING_NEGATION = "missing_negation"
    CORRECTION_NOT_APPLIED = "correction_not_applied"
    IMPORTANT_OMISSION = "important_omission"
    TIME_ERROR = "time_error"
    EMOTION_ERROR = "emotion_error"
    INVALID_SOURCE = "invalid_source"
    CATEGORY_MISMATCH = "category_mismatch"
    LOW_MEMORY_VALUE = "low_memory_value"
    DUPLICATION = "duplication"
    SCHEMA_ERROR = "schema_error"
    BUDGET_EXCEEDED = "budget_exceeded"
    UNCLEAR_WORDING = "unclear_wording"


@dataclass(frozen=True, slots=True)
class ReviewIssue:
    severity: ReviewSeverity
    type: DailyIssueType
    field: Literal['title', 'memorable_moments', 'profile_facts', 'intentions', 'commitments', 'reflections',
                   'source_turn_ids', 'session_id', 'root']
    description: str
    source_turn_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DailyReview:
    """pass保留候选；revised返回完整修订版；reject返回null。"""
    review_result: ReviewVerdict
    issues: tuple[ReviewIssue, ...]
    approved_daily: DailyDocument | None
    review_summary: str

    def feedback(self) -> str:
        return "审核退回：" + self.review_summary + "；" + "；".join(issue.description for issue in self.issues)

    def validate(self, candidate: DailyDocument) -> None:
        if self.review_result == ReviewVerdict.PASS:
            if self.issues or self.approved_daily is not None:
                raise InvalidStateError("pass不得修改候选或附带问题")
        elif self.review_result == ReviewVerdict.REJECT:
            if not self.issues or self.approved_daily is not None:
                raise InvalidStateError("reject须说明问题且不得返回正文")
        elif self.review_result == ReviewVerdict.REVISED:
            if not self.issues or self.approved_daily is None:
                raise InvalidStateError("revised须返回问题和完整修订文档")
            if (self.approved_daily.memory_date != candidate.memory_date
                    or self.approved_daily.source_session_ids != candidate.source_session_ids):
                raise InvalidStateError("审核不得修改归属日期和来源session")
        else:
            raise InvalidStateError("未知审核结论")

    @classmethod
    def from_model_data(cls, data: dict, *, candidate: DailyDocument,
                        session_id: str, allowed_turn_ids: set[str]) -> DailyReview:
        if not isinstance(data, dict) or set(data) != {"review_result", "issues", "approved_daily", "review_summary"}:
            raise InvalidStateError("daily审核字段不符合约定")
        if not isinstance(data["issues"], list) or not isinstance(data["review_summary"], str):
            raise InvalidStateError("daily审核问题或说明格式错误")
        issues = []
        fields = {"title", "memorable_moments", "profile_facts", "intentions", "commitments",
                  "reflections", "source_turn_ids", "session_id", "root"}
        try:
            for item in data["issues"]:
                if (not isinstance(item, dict) or set(item) != {"severity", "type", "field", "description", "source_turn_ids"}
                        or item["field"] not in fields):
                    raise InvalidStateError("审核问题结构不符合约定")
                refs = DailyDocument._refs(item["source_turn_ids"], None)
                if not set(refs) <= allowed_turn_ids:
                    raise InvalidStateError("审核问题引用超出允许范围")
                issues.append(ReviewIssue(ReviewSeverity(item["severity"]), DailyIssueType(item["type"]),
                    item["field"], DailyDocument._text(item["description"]), refs))
            verdict = ReviewVerdict(data["review_result"])
        except (TypeError, ValueError, KeyError) as error:
            raise InvalidStateError("daily审核结论或问题格式错误") from error
        approved = None
        if data["approved_daily"] is not None:
            body = data["approved_daily"]
            if not isinstance(body, dict) or set(body) != set(candidate.to_data()):
                raise InvalidStateError("审核修订正文结构错误")
            if body["memory_date"] != candidate.memory_date.isoformat() or body["source_session_ids"] != list(candidate.source_session_ids):
                raise InvalidStateError("审核不得修改归属日期和来源session")
            model_body = {k:v for k,v in body.items() if k not in ("memory_date", "source_session_ids")}
            model_body["session_id"] = session_id
            approved = DailyDocument.from_model_data(model_body, session_id=session_id,
                memory_date=candidate.memory_date, source_session_ids=candidate.source_session_ids,
                allowed_turn_ids=allowed_turn_ids)
        result = cls(verdict, tuple(issues), approved, data["review_summary"])
        result.validate(candidate)
        return result
