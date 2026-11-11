"""Topic 逐篇审核结论与整批审核绑定。

TopicApplicationReview 保存申请结论和可选修订正文，TopicReviewResult 绑定候选版本与内容哈希。
校验审核对整批申请的完整覆盖，筛选通过项并应用审核修订，供后续准备发布正文。
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from module.memory_management.shared.contracts.review import ReviewVerdict
from module.memory_management.topic.core.domain.document import TopicDocument
from module.memory_management.topic.core.domain.generation import TopicApplication, TopicBatchResult


@dataclass(frozen=True, slots=True)
class TopicApplicationReview:
    """逐份审核；一篇被拒绝不影响其他申请的审核结果。"""
    application_id: str
    review_result: ReviewVerdict
    issues: tuple[str, ...] = ()
    approved_document: TopicDocument | None = None

    def __post_init__(self) -> None:
        if not self.application_id or self.review_result not in tuple(ReviewVerdict):
            raise ValueError("topic审核需要有效申请ID和结论")
        if (self.review_result == ReviewVerdict.REVISED) != (self.approved_document is not None):
            raise ValueError("仅修订通过时返回完整approved_document")


@dataclass(frozen=True, slots=True)
class TopicReviewResult:
    result_version: int
    result_hash: str
    application_reviews: tuple[TopicApplicationReview, ...]

    def __post_init__(self) -> None:
        ids = [item.application_id for item in self.application_reviews]
        if len(set(ids)) != len(ids):
            raise ValueError("同一topic申请不能有多份审核结论")

    def validate(self, result: TopicBatchResult) -> None:
        if self.result_version != result.result_version or self.result_hash != result.content_hash:
            raise ValueError("topic审核未绑定当前批次候选")
        if {item.application_id for item in self.application_reviews} != {item.application_id for item in result.applications}:
            raise ValueError("topic审核必须逐项覆盖当前批次的全部申请")

    def accepted_applications(self, result: TopicBatchResult) -> tuple[TopicApplication, ...]:
        self.validate(result)
        reviews = {item.application_id: item for item in self.application_reviews}
        accepted = []
        for app in result.applications:
            review = reviews[app.application_id]
            if review.review_result == ReviewVerdict.PASS:
                accepted.append(app)
            elif review.review_result == ReviewVerdict.REVISED:
                accepted.append(replace(app, document=review.approved_document))
        return tuple(accepted)
