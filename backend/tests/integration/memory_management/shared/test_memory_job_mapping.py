"""memory_record job在真实SQLite JSON往返中保留全部阶段类型和来源。"""
from dataclasses import replace
from datetime import date, datetime, timezone
import json

import pytest

from module.memory_management.daily.core.domain.generation import DailyCandidate
from module.memory_management.daily.core.domain.plan import DailyPlan
from module.memory_management.daily.core.domain.review import DailyReview
from module.memory_management.daily.core.domain.document import DailyDocument, DailyMoment, MomentKind, DailyReflection, DailyProfileFact, ProfileFactKind, DailyIntention, IntentionOwner, IntentionCertainty, IntentionStatus
from module.shared.context.types.turns import TurnRange
from module.shared.context.types.references import InheritedContextRef, SnapshotRef
from module.memory_management.object.core.domain.job import ObjectJob, ObjectJobInput
from module.memory_management.daily.core.domain.job import DailyJob, DailyJobInput
from module.memory_management.topic.core.domain.job import TopicJob, TopicJobInput
from module.memory_management.shared.jobs.core.domain.job import MemoryJob, MemoryJobInput, MemoryJobStatus, MemoryJobType
from module.memory_management.shared.contracts.source import MemorySourceManifest
from module.memory_management.daily.core.domain.review import DailyIssueType, ReviewIssue
from module.memory_management.shared.contracts.review import ReviewSeverity, ReviewVerdict
from module.memory_management.object.core.domain.document import MemoryObject, ObjectEvent, ObjectPlan, ObjectState
from module.memory_management.object.core.domain.generation import ObjectSelectionNote
from module.memory_management.topic.core.domain.generation import TopicAction, TopicApplication, TopicBatchResult, TopicOperation, TopicOperationKind
from module.memory_management.topic.core.domain.plan import TopicPlan
from module.memory_management.topic.core.domain.review import TopicApplicationReview, TopicReviewResult
from module.memory_management.topic.core.domain.document import TopicCurrentState, TopicDocument, TopicEntry, TopicIdentity
from module.shared.memory.types.indexing import PersonRef
from module.memory_management.shared.core.domain.document import VectorDocument
from module.shared.memory.types.references import EvidenceRef, MemoryKind, MemoryRevisionRef
from module.shared.memory.types.retention import RecallTier
from module.memory_management.daily.repository import mappers as daily_mappers
from module.memory_management.shared.jobs.repository import mappers as job_mappers
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import MemoryJobRow


def make_job(variant):
    now = datetime(2026, 10, 30, 8, tzinfo=timezone.utc)
    day = date(2026, 10, 30)
    evidence = EvidenceRef("s", "m", "决定休息", "turn", 1, "source-hash")
    daily = DailyDocument(day, ("s", "s2"), "日常",
        (DailyMoment(MomentKind.EMOTIONAL_ARC, "用户感到疲惫", ("turn",)),),
        (DailyProfileFact(ProfileFactKind.PREFERENCE, "用户重视项目质量", ("turn",), ("决定休息",)),),
        (DailyIntention(IntentionOwner.USER, IntentionCertainty.PLAN, IntentionStatus.OPEN, "用户打算休息", None, ("turn",), ("我打算休息",)),),
        (), (DailyReflection("回想起来，我想更认真听他讲。", ("turn",)),))
    event = TopicEntry("e", "决定休息", None, now.date(), (evidence,))
    topic = TopicDocument("休息", TopicIdentity("休息安排", (evidence,)),
        TopicCurrentState("准备休息", ("e",)), (event,))
    obj = MemoryObject("obj", "c", "休息",
        (ObjectEvent("user", "决定休息", now.date(), None, (evidence,)),), "s:m", now, forgetfulness=0)
    application = TopicApplication("app", "doc", TopicAction.CREATE if variant == "topic" else TopicAction.UPDATE,
        ("obj",), topic, base_ref=None if variant == "topic" else MemoryRevisionRef(MemoryKind.TOPIC, "doc", 1, "old"),
        discussion_session_ids=("s", "s2", "s3"), operations=(
            TopicOperation(TopicOperationKind.ADD_EVENT, ("obj",), (evidence,), proposed_entry=event),))
    document = daily if variant == "daily" else topic
    kind = MemoryKind.DAILY if variant == "daily" else MemoryKind.TOPIC
    ref = MemoryRevisionRef(kind, "doc", 2, "hash")
    source_daily = MemoryRevisionRef(MemoryKind.DAILY, "daily", 1, "daily-hash")
    common_input = dict(target_kind=kind, target_id="doc", base_revision=1,
        base_content_hash="old", target_revision=2, edited_document=document)
    job_input = (DailyJobInput("c", ("s", "s2"), source_message_ids=("m",), memory_date=day, **common_input)
        if variant == "daily" else TopicJobInput("c", ("s", "s2"),
            object_ids=("obj",),
            **common_input))
    candidate = DailyCandidate(1, daily, "hash", now) if variant == "daily" else TopicBatchResult(1, (application,), (), "hash", "prompt", now)
    review = DailyReview(ReviewVerdict.REVISED,
        (ReviewIssue(ReviewSeverity.MAJOR, DailyIssueType.TIME_ERROR, "root", "修正时间", ("turn",)),),
        daily, "已修正") if variant == "daily" else TopicReviewResult(1, "hash", (TopicApplicationReview("app", ReviewVerdict.PASS),))
    if variant == "daily":
        job_class = DailyJob
        specific = dict(reextraction_count=1,
            plan=DailyPlan("s", "c", day, TurnRange("s", 3, 4), TurnRange("s", 1, 2),
                MemoryRevisionRef(MemoryKind.DAILY, "daily", 1, "daily-hash"),
                InheritedContextRef("previous", SnapshotRef("snapshot", "summary-hash", 5),
                    TurnRange("previous", 6, 7), "inherited-hash")))
    else:
        job_class = TopicJob
        specific = dict(plan=TopicPlan("c", ("obj",), topic_refs=(application.base_ref,) if application.base_ref else ()))
    return job_class("j", MemoryJobType.CREATE_DAILY if variant == "daily" else MemoryJobType.CONSOLIDATE_TOPIC,
        "target", "dedupe", 10, MemoryJobStatus.DONE, now, job_input, now, now,
        retry_count=2, **({"candidate": candidate} if variant == "daily" else {"generation_result": candidate}), review=review,
        vector_document=VectorDocument("vector", ref, "c", document, (PersonRef("person", "user"),),
            ("rest",), "preview", now, now),
        storage_job_id="storage", result=ref, last_error="old error", finished_at=now,
        reviewed_version=1, reviewed_hash="hash",
        applied_revision=2, error_code="old_code", attempt_version=3, attempt_count=2,
        source_manifest=MemorySourceManifest("c", ("s", "s2"), ("m",), "source-hash", day,
            (source_daily,), MemoryRevisionRef(kind, "doc", 1, "old")), **specific)



@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["daily", "topic", "update"])
async def test_memory_job_full_json_roundtrip(tmp_path, variant):
    database = SQLiteDatabase(tmp_path / "memory_record.sqlite3")
    try:
        await database.initialize()
        job = make_job(variant)
        # json.dumps也必须成功，不能残留date、datetime、Enum或数据类实例。
        json.dumps(job_mappers.memory_job_payload_to_json(job), ensure_ascii=False)
        async with database.transaction() as session:
            session.add(job_mappers.memory_job_to_row(job))
        async with database.db_session() as session:
            row = await session.get(MemoryJobRow, "j")
            restored = job_mappers.memory_job_to_domain(row)
        assert restored == job
        assert isinstance(restored.input.source_session_ids, tuple)
        if isinstance(restored, DailyJob):
            assert type(restored.input.memory_date) is date
    finally:
        await database.close()


def test_new_daily_job_preserves_empty_and_optional_fields():
    full = make_job("daily")
    job = DailyJob("new", MemoryJobType.CREATE_DAILY, "target", "dedupe", 10,
        MemoryJobStatus.NEED_GENERATE, full.created_at,
        DailyJobInput("c", ("s",), memory_date=full.input.memory_date), full.created_at, full.created_at)
    assert job_mappers.memory_job_to_domain(job_mappers.memory_job_to_row(job)) == job
    full = replace(full, candidate=replace(full.candidate, document=replace(full.candidate.document,
        memorable_moments=(), reflections=())), review=replace(full.review, approved_daily=None))
    assert job_mappers.memory_job_to_domain(job_mappers.memory_job_to_row(full)) == full


@pytest.mark.parametrize("bad_version", [0, 1, 2, 3, 4, 5, True, "6"])
def test_unknown_schema_fails_explicitly(bad_version):
    row = job_mappers.memory_job_to_row(make_job("daily"))
    row.payload_json["schema_version"] = bad_version
    with pytest.raises(ValueError, match="schema"):
        job_mappers.memory_job_to_domain(row)


def test_missing_fields_and_unknown_variants_fail_explicitly():
    row = job_mappers.memory_job_to_row(make_job("daily"))
    del row.payload_json["attempt_version"]
    with pytest.raises(KeyError):
        job_mappers.memory_job_to_domain(row)
    row = job_mappers.memory_job_to_row(make_job("daily"))
    row.payload_json["job_family"] = "unknown"
    with pytest.raises(ValueError, match="family"):
        job_mappers.memory_job_to_domain(row)


def test_daily_plan_minimal_and_inherited_references_roundtrip():
    first = DailyPlan("s", "c", date(2026, 10, 30), TurnRange("s", 1, 2))
    assert daily_mappers.daily_plan_from_json(daily_mappers.daily_plan_to_json(first)) == first
    assert daily_mappers.daily_plan_to_json(None) is None
    assert daily_mappers.daily_plan_from_json(None) is None
    rich = make_job("daily").plan
    data = json.loads(json.dumps(daily_mappers.daily_plan_to_json(rich)))
    assert data["memory_date"] == "2026-10-30"
    assert data["source_turns"] == {"session_id": "s", "from_seq": 3, "through_seq": 4}
    assert daily_mappers.daily_plan_from_json(data) == rich
    assert rich.base_daily_ref.revision == 1


@pytest.mark.parametrize("changes", [
    {"source_turns": None},
    {"source_turns": TurnRange("other", 3, 4)},
    {"source_turns": TurnRange("s", 4, 3)},
    {"context_only_turns": TurnRange("s", 1, 3)},
    {"base_daily_ref": MemoryRevisionRef(MemoryKind.TOPIC, "topic", 1, "hash")},
    {"inherited_context": InheritedContextRef("s", None, None, None)},
])
def test_daily_plan_rejects_mixed_source_or_future_context(changes):
    with pytest.raises(ValueError):
        replace(make_job("daily").plan, **changes)


def test_memory_job_requires_current_plan_field_and_schema():
    row = job_mappers.memory_job_to_row(make_job("daily"))
    assert row.payload_json["schema_version"] == 6
    del row.payload_json["plan"]
    with pytest.raises(KeyError):
        job_mappers.memory_job_to_domain(row)


def test_daily_and_topic_payloads_do_not_carry_each_others_fields():
    daily = job_mappers.memory_job_payload_to_json(make_job("daily"))
    topic = job_mappers.memory_job_payload_to_json(make_job("topic"))
    assert daily["job_family"] == "daily" and topic["job_family"] == "topic"
    assert "memory_objects" not in daily
    assert "topic_application" not in daily["input"] and "source_daily_refs" not in daily["input"]
    assert "plan" in topic and "reextraction_count" not in topic
    assert "memory_date" not in topic["input"] and "source_message_ids" not in topic["input"]
    assert "type" not in daily["candidate"] and "type" not in topic["generation_result"]
    assert isinstance(job_mappers.memory_job_to_domain(job_mappers.memory_job_to_row(make_job("daily"))), DailyJob)
    assert isinstance(job_mappers.memory_job_to_domain(job_mappers.memory_job_to_row(make_job("topic"))), TopicJob)


def test_object_extraction_has_independent_family_and_session_input():
    now = datetime.now(timezone.utc)
    evidence = EvidenceRef("s", "m", "决定休息", "turn", 1, "hash")
    obj = MemoryObject("obj", "c", "休息",
        (ObjectEvent("user", "决定休息", now.date(), None, (evidence,)),), "s:m", now, forgetfulness=0)
    extract = ObjectJob("object-job", MemoryJobType.EXTRACT_OBJECTS, "object:c:s", "object:c:s", 10,
        MemoryJobStatus.NEED_GENERATE, now, ObjectJobInput("c", ("s",)), now, now,
        objects=(obj,))
    row = job_mappers.memory_job_to_row(extract)
    assert row.payload_json["job_family"] == "object"
    assert "source_daily_refs" not in row.payload_json["input"]
    assert job_mappers.memory_job_to_domain(row) == extract
    assert extract.objects[0].forgetfulness == 0
    assert not hasattr(extract, "plan") and not hasattr(extract, "candidate")


@pytest.mark.parametrize("variant", ["daily", "topic"])
def test_user_edits_dispatch_by_family_not_only_job_type(variant):
    value = replace(make_job(variant), job_type=MemoryJobType.USER_EDIT)
    assert type(job_mappers.memory_job_to_domain(job_mappers.memory_job_to_row(value))) is type(value)


def test_family_mismatch_is_rejected_instead_of_losing_fields():
    daily, topic = make_job("daily"), make_job("topic")
    with pytest.raises(ValueError):
        replace(daily, input=topic.input)
    with pytest.raises(ValueError):
        replace(topic, generation_result=daily.candidate)
    row = job_mappers.memory_job_to_row(daily)
    row.job_type = MemoryJobType.UPDATE_TOPIC.value
    with pytest.raises(ValueError):
        job_mappers.memory_job_to_domain(row)


@pytest.mark.asyncio
async def test_topic_source_change_clears_only_attempt_artifacts(tmp_path):
    from module.memory_management.topic.repository.sqlite import TopicProductionRepository

    database = SQLiteDatabase(tmp_path / "topic-retry.sqlite3")
    try:
        await database.initialize()
        value = replace(make_job("topic"), status=MemoryJobStatus.NEED_REVIEW,
            result=None, applied_revision=None)
        async with database.transaction() as session:
            session.add(job_mappers.memory_job_to_row(value))
        repo = TopicProductionRepository(database)
        assert await repo.retry_job(value.job_id, value.attempt_version,
            expected_status=value.status, expected_retry_count=value.retry_count,
            available_at=datetime.now(timezone.utc), error="source changed", restart_generation=True)
        reset = await repo.get_job(value.job_id)
        assert isinstance(reset, TopicJob) and reset.input == value.input
        assert reset.generation_result is reset.review is reset.plan is None
        assert reset.source_manifest is reset.vector_document is reset.storage_job_id is None
        assert reset.attempt_version == value.attempt_version+1 and reset.retry_count == value.retry_count+1
        assert reset.status == MemoryJobStatus.NEED_GENERATE
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_object_events_and_debug_notes_roundtrip_through_sqlite(tmp_path):
    """多日进展、未知发生日与入库时间不能被JSON往返混为一谈。"""
    created_at = datetime(2026, 10, 31, 8, tzinfo=timezone.utc)
    events = (
        ObjectEvent("user", "申请资料仍在准备中", date(2026, 8, 6), None,
                    (EvidenceRef("s", "m1", "", "t1", 2, "h1"),)),
        ObjectEvent("user", "申请已提交", date(2026, 8, 9), date(2026, 8, 8),
                    (EvidenceRef("s", "m2", "", "t2", 1, "h2"),)),
    )
    plans = (
        ObjectPlan("user", "打算次日提交申请", date(2026, 8, 6), date(2026, 8, 7),
                   (EvidenceRef("s", "m1", "", "t1", 2, "h1"),)),
        ObjectPlan("user", "打算继续补充申请材料，时间未定", date(2026, 8, 9), None,
                   (EvidenceRef("s", "m2", "", "t2", 1, "h2"),)),
    )
    candidate = MemoryObject("obj", "c", "申请事项", events, "s:t1:t2", created_at,
        plans=plans, forgetfulness=0, state=ObjectState.LINKED, recall_tier=RecallTier.COLD, linked_topic_id="topic1")
    notes = (ObjectSelectionNote("提交计划与结果", "keep", "不同日期的新进展分别记录", ("m1", "m2")),)
    job = ObjectJob("object", MemoryJobType.EXTRACT_OBJECTS, "object:c:s", "extract_objects:c:s", 10,
        MemoryJobStatus.NEED_INDEX, created_at, ObjectJobInput("c", ("s",)), created_at, created_at,
        objects=(candidate,), selection_notes=notes)
    payload = json.loads(json.dumps(job_mappers.memory_job_payload_to_json(job), ensure_ascii=False))
    stored_candidate = payload["objects"][0]
    assert set(stored_candidate).isdisjoint({"object_mentions", "actor", "statement", "acquire_time", "occurrence_time", "source_refs", "selection_notes"})
    assert stored_candidate["events"][0]["occurrence_time"] is None
    assert stored_candidate["events"][1]["acquire_time"] == "2026-08-09"
    assert stored_candidate["events"][1]["occurrence_time"] == "2026-08-08"
    assert stored_candidate["plans"][0]["target_time"] == "2026-08-07"
    assert stored_candidate["plans"][1]["target_time"] is None
    assert "occurrence_time" not in stored_candidate["plans"][0]
    assert "target_time" not in stored_candidate["events"][0]
    database = SQLiteDatabase(tmp_path / "object-events.sqlite3")
    try:
        await database.initialize()
        async with database.transaction() as session:
            row = job_mappers.memory_job_to_row(job)
            row.payload_json = payload
            session.add(row)
        async with database.db_session() as session:
            restored = job_mappers.memory_job_to_domain(await session.get(MemoryJobRow, job.job_id))
        assert restored == job
        assert type(restored.objects[0].events[0].acquire_time) is date
        assert type(restored.objects[0].created_at) is datetime
        assert type(restored.objects[0].plans[0].target_time) is date
        assert restored.selection_notes == notes
        # 索引中断后恢复到候选检查点，不能丢失生成时的进展和调试材料。
        async with database.transaction() as session:
            (await session.get(MemoryJobRow, job.job_id)).status = MemoryJobStatus.INDEXING.value
        from module.memory_management.object.repository.sqlite import ObjectProductionRepository
        repository = ObjectProductionRepository(database)
        assert await repository.recover_unfinished_attempts() == 1
        recovered = await repository.get_job(job.job_id)
        assert recovered.status == MemoryJobStatus.NEED_INDEX
        assert recovered.objects == (candidate,) and recovered.selection_notes == notes
    finally:
        await database.close()


def test_object_event_rejects_datetime_and_candidate_rejects_empty_events():
    now = datetime.now(timezone.utc)
    evidence = EvidenceRef("s", "m", "")
    with pytest.raises(ValueError, match="日期"):
        ObjectEvent("user", "已提交", now, None, (evidence,))
    with pytest.raises(ValueError, match="进展"):
        MemoryObject("obj", "c", "申请事项", (), "s:m", now)
    event = ObjectEvent("user", "打算提交", now.date(), None, (evidence,))
    with pytest.raises(ValueError, match="forgetfulness"):
        MemoryObject("obj", "c", "申请事项", (event,), "s:m", now, forgetfulness=True)


@pytest.mark.parametrize("target_time", [None, date(2026, 11, 1)])
def test_plan_only_candidate_roundtrip_and_group_types(target_time):
    now = datetime(2026, 10, 31, 8, tzinfo=timezone.utc)
    refs = (EvidenceRef("s", "m", ""),)
    plan = ObjectPlan("user", "打算提交申请", now.date(), target_time, refs)
    candidate = MemoryObject("obj", "c", "申请事项", (), "s:m", now, plans=(plan,), forgetfulness=0)
    job = ObjectJob("object", MemoryJobType.EXTRACT_OBJECTS, "object:c:s", "extract_objects:c:s", 10,
        MemoryJobStatus.NEED_INDEX, now, ObjectJobInput("c", ("s",)), now, now,
        objects=(candidate,))
    row = job_mappers.memory_job_to_row(job)
    row.payload_json = json.loads(json.dumps(row.payload_json))
    assert row.payload_json["objects"][0]["events"] == []
    assert job_mappers.memory_job_to_domain(row) == job
    with pytest.raises(ValueError, match="events"):
        replace(candidate, events=(plan,))
    event = ObjectEvent("user", "申请已提交", now.date(), now.date(), refs)
    with pytest.raises(ValueError, match="plans"):
        replace(candidate, plans=(event,))
    with pytest.raises(ValueError, match="日期"):
        replace(plan, target_time=now)


@pytest.fixture
def topic_batch():
    """一批包含新建、更新和继续暂存，历史object不占本批名额。"""
    now = datetime(2026, 11, 1, 8, tzinfo=timezone.utc)
    ref = MemoryRevisionRef(MemoryKind.TOPIC, "existing", 2, "old-hash")
    evidence = EvidenceRef("s", "u", "下周开始", "t")
    plan_entry = TopicEntry("plan", "打算下周开始", None, now.date(), (evidence,),
                            kind="plan", target_time=date(2026, 11, 8))
    happened = TopicEntry("event", "项目已建立", date(2026, 10, 31), now.date(), (evidence,))
    document = TopicDocument("项目", TopicIdentity("持续跟踪该项目", (evidence,)),
                             TopicCurrentState("准备开始", ("plan", "event")), (plan_entry, happened))
    applications = (
        TopicApplication("create", "new-topic", TopicAction.CREATE, ("new1", "history"), document,
                         discussion_session_ids=("s", "s2", "s3")),
        TopicApplication("update", "existing", TopicAction.UPDATE, ("new2",), document, base_ref=ref),
    )
    candidate = TopicBatchResult(1, applications, ("new3",), "batch-hash", "prompt-hash", now)
    plan = TopicPlan("c", ("new1", "new2", "new3"), ("history",), (ref,))
    return plan, candidate, document


@pytest.mark.asyncio
async def test_topic_batch_plan_applications_and_mixed_reviews_roundtrip(tmp_path, topic_batch):
    plan, candidate, document = topic_batch
    candidate.validate_against_plan(plan)
    review = TopicReviewResult(1, candidate.content_hash, (
        TopicApplicationReview("create", ReviewVerdict.REVISED, ("修正措辞",), replace(document, topic="新项目")),
        TopicApplicationReview("update", ReviewVerdict.REJECT, ("归属依据不足",)),
    ))
    now = candidate.generated_at
    value = TopicJob("batch", MemoryJobType.CONSOLIDATE_TOPIC, "topic:c:consolidation", "batch:ids", 10,
        MemoryJobStatus.NEED_WRITE, now, TopicJobInput("c", ("s",), object_ids=plan.object_ids), now, now,
        plan=plan, generation_result=candidate, review=review)
    database = SQLiteDatabase(tmp_path / "batch.sqlite3")
    try:
        await database.initialize()
        payload = json.loads(json.dumps(job_mappers.memory_job_payload_to_json(value)))
        assert "topic_application" not in payload["input"]
        assert "document" not in payload["generation_result"]
        assert payload["generation_result"]["applications"][0]["base_ref"] is None
        assert payload["generation_result"]["applications"][1]["base_ref"]["revision"] == 2
        entry = payload["generation_result"]["applications"][0]["document"]["entries"][0]
        assert entry["kind"] == "plan" and entry["occurrence_time"] is None
        assert entry["target_time"] == "2026-11-08" and entry["acquire_time"] == "2026-11-01"
        async with database.transaction() as session:
            row = job_mappers.memory_job_to_row(value)
            row.payload_json = payload
            session.add(row)
        async with database.db_session() as session:
            restored = job_mappers.memory_job_to_domain(await session.get(MemoryJobRow, "batch"))
        assert restored == value
        restored.review.validate(restored.generation_result)
        accepted = restored.review.accepted_applications(restored.generation_result)
        assert len(accepted) == 1 and accepted[0].application_id == "create"
        assert accepted[0].document.topic == "新项目"
        assert restored.generation_result.applications[0].document.topic == "项目"
    finally:
        await database.close()


@pytest.mark.parametrize("case", ["duplicate_object", "duplicate_target", "used_and_deferred", "missing_object", "foreign_object", "foreign_base"])
def test_topic_batch_rejects_invalid_assignment(topic_batch, case):
    plan, candidate, _ = topic_batch
    first, second = candidate.applications
    with pytest.raises(ValueError):
        if case == "duplicate_object":
            replace(candidate, applications=(first, replace(second, object_ids=first.object_ids)))
        elif case == "duplicate_target":
            replace(candidate, applications=(first, replace(first, application_id="other")))
        elif case == "used_and_deferred":
            replace(candidate, pending_object_ids=("new1", "new3"))
        elif case == "missing_object":
            replace(candidate, pending_object_ids=()).validate_against_plan(plan)
        elif case == "foreign_object":
            replace(candidate, pending_object_ids=("foreign",)).validate_against_plan(plan)
        else:
            replace(candidate, applications=(first, replace(second,
                base_ref=replace(second.base_ref, revision=9)))).validate_against_plan(plan)


@pytest.mark.parametrize("case", ["missing_review", "duplicate_review", "wrong_hash", "revised_without_body", "pass_with_body"])
def test_topic_batch_review_is_bound_and_complete(topic_batch, case):
    _, candidate, document = topic_batch
    first = TopicApplicationReview("create", ReviewVerdict.PASS)
    second = TopicApplicationReview("update", ReviewVerdict.REJECT)
    with pytest.raises(ValueError):
        if case == "duplicate_review":
            TopicReviewResult(1, "batch-hash", (first, first))
        elif case == "revised_without_body":
            TopicApplicationReview("create", ReviewVerdict.REVISED)
        elif case == "pass_with_body":
            replace(first, approved_document=document)
        else:
            reviews = (first,) if case == "missing_review" else (first, second)
            digest = "wrong" if case == "wrong_hash" else candidate.content_hash
            TopicReviewResult(1, digest, reviews).validate(candidate)


def test_topic_source_has_related_objects_and_formal_topics_without_inherited_context(topic_batch):
    from dataclasses import asdict
    from module.memory_management.topic.core.domain.source import TopicSourceInput, TopicObjectGroup
    from module.memory_management.topic.core.domain.source import TopicContext
    from module.memory_management.topic.core.processing.config import TopicProductionConfig
    plan, candidate, document = topic_batch
    now = candidate.generated_at
    entry = document.entries[1]
    def object_item(cid):
        return MemoryObject(cid, "c", "项目", (ObjectEvent("user", entry.statement,
            entry.acquire_time, entry.occurrence_time, entry.source_refs),), cid, now)
    groups = tuple(TopicObjectGroup(object_item(cid), (object_item("history"),),
        (TopicContext(plan.topic_refs[0], document),)) for cid in plan.object_ids)
    source = TopicSourceInput(plan, groups)
    payload = source.to_model_payload()
    json.dumps(payload, default=str)
    assert "INHERITED_CONTEXT" not in payload and "SOURCE_SESSIONS" not in payload
    assert len(payload["GROUPS"]) == 3
    assert all(len(group["RELATED_OBJECTS"]) == len(group["RELATED_TOPICS"]) == 1 for group in payload["GROUPS"])
    assert [group["OBJECT"]["object_id"] for group in payload["GROUPS"]] == list(plan.object_ids)
    expected_document = asdict(document)
    # 模型输入会按时间窗口筛选事件，使用 JSON 数组形式；本样例事件均在窗口内。
    expected_document["entries"] = list(expected_document["entries"])
    assert all(group["RELATED_TOPICS"][0]["document"] == expected_document for group in payload["GROUPS"])
    assert not {"CANDIDATES", "RELATED_CANDIDATES", "EXISTING_TOPICS", "RECALL_MATCHES"} & payload.keys()
    assert TopicProductionConfig().max_batch_objects == 5
    assert TopicProductionConfig(max_batch_objects=10).max_batch_objects == 10
    with pytest.raises(ValueError):
        replace(source, groups=tuple(replace(group, related_objects=()) for group in groups))
    with pytest.raises(ValueError):
        replace(source, groups=(replace(groups[0], object=replace(groups[0].object, character_id="other")), *groups[1:]))
    with pytest.raises(ValueError):
        TopicProductionConfig(max_batch_objects=0)


@pytest.mark.parametrize("case", ["plan_occurrence", "event_target", "datetime_acquire", "missing_entry"])
def test_topic_timeline_does_not_confuse_plan_and_event_dates(topic_batch, case):
    _, _, document = topic_batch
    with pytest.raises(ValueError):
        if case == "plan_occurrence":
            replace(document.entries[0], occurrence_time=date(2026, 11, 8))
        elif case == "event_target":
            replace(document.entries[1], target_time=date(2026, 11, 8))
        elif case == "datetime_acquire":
            replace(document.entries[0], acquire_time=datetime.now(timezone.utc))
        else:
            replace(document, current_state=TopicCurrentState("缺少引用", ("missing",)))


def test_topic_empty_batch_result_defers_all_without_fake_topic(topic_batch):
    plan, candidate, _ = topic_batch
    deferred = replace(candidate, applications=(), pending_object_ids=plan.object_ids)
    deferred.validate_against_plan(plan)
    review = TopicReviewResult(1, deferred.content_hash, ())
    assert review.accepted_applications(deferred) == ()
    minimal = TopicJob("new", MemoryJobType.CONSOLIDATE_TOPIC, "target", "dedupe", 10,
        MemoryJobStatus.NEED_GENERATE, candidate.generated_at,
        TopicJobInput("c", ("s",), object_ids=plan.object_ids), candidate.generated_at, candidate.generated_at)
    assert job_mappers.memory_job_to_domain(job_mappers.memory_job_to_row(minimal)) == minimal
