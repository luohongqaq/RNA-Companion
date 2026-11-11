"""Daily 持久化任务输入、阶段产物及发布前校验。

DailyJobInput 固定会话、消息范围和归属日期；DailyJob 保存计划、候选、审核与待发布结果。
任务输入构造登记命令，任务本身核对类型、审核绑定和索引版本。
"""
from __future__ import annotations
from module.shared.common.errors.base import InvalidStateError
from module.memory_management.shared.core.domain.document import VectorDocument
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from dataclasses import dataclass
from datetime import date
from module.memory_management.daily.core.domain.document import DailyDocument
from module.memory_management.daily.core.domain.generation import DailyCandidate
from module.memory_management.daily.core.domain.plan import DailyPlan
from module.memory_management.daily.core.domain.publication import DailyWrite
from module.memory_management.daily.core.domain.review import DailyReview
from module.memory_management.shared.jobs.core.domain.job import CreateMemoryJob, MemoryJob, MemoryJobInput, MemoryJobType
from module.memory_management.shared.contracts.review import ReviewVerdict


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyJobInput(MemoryJobInput):
    """daily输入：封存原文及按完整轮确定的归属日期。"""
    source_message_ids: tuple[str, ...] = ()
    target_kind: MemoryKind | None = MemoryKind.DAILY
    edited_document: DailyDocument | None = None
    memory_date: date | None = None  # daily归属日：每轮第一条用户消息的后端当地日期

    @classmethod
    def create_command(cls, character_id: str, session_id: str, memory_date: date,
                       source_message_ids: tuple[str, ...], *, priority: int = 10) -> CreateMemoryJob:
        """为一个封存session的一个日期组构造登记输入；不提前绑定daily基础版本。"""
        return CreateMemoryJob(
            job_type=MemoryJobType.CREATE_DAILY,
            target_key=f'daily:{character_id}:{memory_date.isoformat()}',
            dedupe_key=f'create_daily:{character_id}:{session_id}:{memory_date.isoformat()}',
            priority=priority,
            input=cls(character_id=character_id, source_session_ids=(session_id,),
                source_message_ids=source_message_ids, target_kind=MemoryKind.DAILY, memory_date=memory_date),
        )


@dataclass(slots=True)
class DailyJob(MemoryJob):
    """daily生成、审核与发布；不承载topic申请及object提取结果。"""
    input: DailyJobInput
    plan: DailyPlan | None = None
    candidate: DailyCandidate | None = None
    review: DailyReview | None = None
    reextraction_count: int = 0
    write_result: DailyWrite | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.input, DailyJobInput) or self.job_type not in (MemoryJobType.CREATE_DAILY, MemoryJobType.USER_EDIT):
            raise ValueError("DailyJob的任务类型或输入不匹配")
        if ((self.candidate is not None and not isinstance(self.candidate, DailyCandidate))
                or (self.review is not None and not isinstance(self.review, DailyReview))):
            raise ValueError("DailyJob只能保存daily候选和审核结果")

    def final_document(self) -> DailyDocument:
        """审核绑定当前候选；pass取原文，revised取审核修订版。"""
        if self.candidate is None or self.review is None:
            raise InvalidStateError("daily尚无已审核候选")
        if (self.reviewed_version != self.candidate.candidate_version
                or self.reviewed_hash != self.candidate.content_hash):
            raise InvalidStateError("daily审核未绑定当前候选")
        self.review.validate(self.candidate.document)
        if self.review.review_result == ReviewVerdict.PASS:
            return self.candidate.document
        if self.review.review_result == ReviewVerdict.REVISED:
            return self.review.approved_daily
        raise InvalidStateError("审核reject的daily不能准备发布")

    def index_document(self) -> VectorDocument:
        """版本由固定基础版本推导，重启与重试不改变索引身份。"""
        if self.write_result is None or self.plan is None:
            raise InvalidStateError("daily尚无待发布正文或固定计划")
        base = self.plan.base_daily_ref
        # 如果有base_daily，就按原有的base daily推导版本号；无base时从1开始
        ref = MemoryRevisionRef(MemoryKind.DAILY, base.memory_id if base else self.target_key,
                                base.revision + 1 if base else 1, self.write_result.content_hash)
        return VectorDocument(f"{ref.memory_id}:r{ref.revision}:{ref.content_hash}", ref,
            self.input.character_id, self.write_result.document, (), (),
            self.write_result.document.title)
