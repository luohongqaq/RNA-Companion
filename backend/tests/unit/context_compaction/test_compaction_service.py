"""Unified handoff source coverage, fixed incoming context and legacy JSON reads."""
from dataclasses import asdict, replace
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from module.shared.context.types.turns import TurnRange
from module.character.core.domain.models import CharacterCard
from module.shared.common.errors.base import InvalidStateError
from module.context_compaction.core.domain.policy import ContextCompactionPolicy
from module.context_compaction.core.domain.lifecycle_policy import SessionLifecycleConfig
from module.context_compaction.repository.llm import ContextSummaryModelRepository
from module.context_compaction.core.processing.service import ContextCompactionService
from module.context_compaction.core.domain.plan import CompactionOutcome as O, TurnTokenInfo
from module.context_compaction.core.domain.lifecycle import LifecycleAction as A
from module.context_compaction.core.domain.source import SummaryModelInput
from module.context_compaction.core.domain.job import ContextJob, ContextJobStatus, ContextJobType
from module.context_compaction.core.domain.references import CompactionMode
from module.shared.context.types.references import InheritedContextRef, SnapshotRef
from module.context_compaction.core.domain.snapshots import (
    ContextSnapshot, SnapShotState, SummarySourceManifest,
)
from module.shared.context.types.summary import Commitment, CommitmentStatus, StructuredSummary, SummaryVersions
from module.chat.reply.core.domain.config.context_budget import ContextPromptBudget
from module.context_compaction.repository import mappers as context_mappers


class SummaryTestCounter:
    tokenizer_version = "test-counter"

    def count_messages(self, messages):
        return sum(len(message.content) for message in messages)


def make_service(**kwargs):
    return ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy(), **kwargs)


def character_resources():
    directory = Path(__file__).resolve().parents[3] / "resources/characters/character_deepseek"
    card = CharacterCard("character_deepseek", "deepseek", (directory / "角色卡.md").read_text(encoding="utf-8-sig"), directory)
    return SimpleNamespace(load=AsyncMock(return_value=SimpleNamespace(character_card=card)))


def model_client(value, finish_reason="stop"):
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
        message=SimpleNamespace(content=json.dumps(value, ensure_ascii=False)))]))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def process_materials(status):
    now = datetime.now(timezone.utc)
    job = ContextJob("job", ContextJobType.PREPARE_HANDOFF, "s", "session:s", "dedupe", 10,
        status, now, 4, now, now)
    frozen_plan, manifest = plan([100] * 4), base(4).source_manifest
    started = replace(job, status=ContextJobStatus.GENERATING, plan=frozen_plan,
        source_manifest=manifest, attempt_version=3, attempt_count=3)
    source = SummaryModelInput(frozen_plan, manifest, (), character_id="character_deepseek")
    repository = SimpleNamespace(get_job=AsyncMock(return_value=job),
        read_inherited_context=AsyncMock(return_value=None), read_turn_token_infos=AsyncMock(return_value=stats([100] * 4)),
        begin_generate=AsyncMock(return_value=started), begin_validate=AsyncMock(), read_model_input=AsyncMock(return_value=source),
        save_candidate=AsyncMock(return_value=True), save_validation=AsyncMock(return_value=True),
        publish_ready_snapshot=AsyncMock())
    return repository, started, source


@pytest.mark.asyncio
async def test_direct_generation_uses_one_call_and_passes_previous_errors():
    from module.shared.context.types.references import ContextSourceRef
    summary = StructuredSummary("有动力做桌宠", source_refs=(ContextSourceRef("t1", 1, "s"),), keywords=("桌宠",))
    client = model_client({"status": "ok", "summary": asdict(summary), "issues": []})
    resources = character_resources()
    service = make_service(model=ContextSummaryModelRepository(client), prompt_resources=resources)
    source = SummaryModelInput(plan([100] * 4), base(4).source_manifest, (), character_id="character_deepseek")
    assert await service.generate_summary(source, feedback="缺少日期") == summary
    client.chat.completions.create.assert_awaited_once()
    assert client.chat.completions.create.await_args.kwargs["reasoning_effort"] == "low"
    assert client.chat.completions.create.await_args.kwargs["extra_body"]["thinking"]["type"] == "enabled"
    payload = json.loads(client.chat.completions.create.await_args.kwargs["messages"][1]["content"])
    assert payload["previous_errors"] == "缺少日期"
    resources.load.assert_awaited_once_with("character_deepseek")
    assert payload["CHARACTER_CONTEXT"]["character_name"] == "deepseek"
    assert "## 身份" in payload["CHARACTER_CONTEXT"]["rules"]
    assert "## 视觉表达" not in payload["CHARACTER_CONTEXT"]["rules"]
    assert not hasattr(service, "review_summary")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_scope", "wrong_card"])
async def test_generation_rejects_missing_or_mismatched_character_before_api_call(failure):
    client = model_client({})
    resources = character_resources()
    service = make_service(model=ContextSummaryModelRepository(client), prompt_resources=resources)
    source = SummaryModelInput(plan([100] * 4), base(4).source_manifest, (),
        character_id=None if failure == "missing_scope" else "another-character")
    card = resources.load.return_value.character_card
    with pytest.raises(InvalidStateError):
        await service.generate_summary(source, character_card=card)
    client.chat.completions.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_character_reader_does_not_start_generation_attempt():
    repo, _, _ = process_materials(ContextJobStatus.NEED_GENERATE)
    service = make_service(repository=repo, token_counter=SummaryTestCounter(), model=ContextSummaryModelRepository(SimpleNamespace()))
    with pytest.raises(NotImplementedError, match="角色资源"):
        await service.process_job("job")
    repo.begin_generate.assert_not_awaited()


@pytest.mark.parametrize("failure", ["no_keywords", "duplicate_keywords", "no_date", "invalid_date", "placeholder", "no_refs"])
def test_model_boundary_rejects_missing_or_invalid_fields(failure):
    value = {"summary": "当前状态", "source_refs": [{"turn_id": "t1", "revision": 1, "session_id": "s"}], "keywords": ["桌宠"]}
    if failure == "no_keywords":
        value.pop("keywords")
    elif failure == "duplicate_keywords":
        value["keywords"] = ["RAG", "rag"]
    elif failure in ("no_date", "invalid_date"):
        value["commitments"] = [{"commitment_id": "c1", "role": "user", "text": "整理项目", "revision": 1,
            "status": "active", "source_refs": value["source_refs"],
            "decided_on": None if failure == "no_date" else "2026-02-30"}]
    elif failure == "placeholder":
        value["tone_context"] = [{"emotion": "迷茫", "temporal_context": None,
            "current_status": "未知或null", "source_refs": value["source_refs"]}]
    else:
        value["source_refs"] = []
    with pytest.raises(InvalidStateError):
        StructuredSummary.from_model_data(value)


def test_dates_keywords_mapping_hash_and_rendering():
    from module.shared.context.types.references import ContextSourceRef
    refs = (ContextSourceRef("t1", 1, "s"),)
    summary = StructuredSummary("用户决定整理项目", commitments=(Commitment("c1", "user", "整理项目", 1,
        CommitmentStatus.ACTIVE, refs, date(2026, 9, 12), date(2026, 9, 13)),),
        source_refs=refs, keywords=("桌宠", "RAG"))
    encoded = context_mappers.structured_summary_to_json(summary)
    assert encoded["commitments"][0]["decided_on"] == "2026-09-12"
    assert context_mappers.structured_summary_from_json(encoded) == summary
    parsed = StructuredSummary.from_model_data(encoded)
    assert parsed == summary
    rendered = summary.render()
    assert "2026-09-12" in rendered and "2026-09-13" in rendered
    assert json.loads(rendered)["聊天关键词"] == ["桌宠", "RAG"]
    assert "source_refs" not in rendered
    changed = replace(summary, keywords=("面试",))
    assert changed.render() != rendered
    assert changed.content_hash() != summary.content_hash()


@pytest.mark.asyncio
async def test_process_generate_saves_candidate_and_forwards_retry_feedback():
    repo, started, source = process_materials(ContextJobStatus.NEED_GENERATE)
    repo.get_job.return_value = replace(repo.get_job.return_value, last_error="日期缺失")
    service = make_service(repository=repo, token_counter=SummaryTestCounter(), model=ContextSummaryModelRepository(SimpleNamespace()),
        prompt_resources=character_resources())
    service.generate_summary = AsyncMock(return_value=StructuredSummary("generated", keywords=("桌宠",)))
    await service.process_job("job")
    service.generate_summary.assert_awaited_once_with(source, feedback="日期缺失")
    _, attempt, candidate = repo.save_candidate.await_args.args
    assert attempt == candidate.attempt_version == started.attempt_version
    repo.save_validation.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_malformed_generation_schedules_retry_without_candidate():
    repo, _, _ = process_materials(ContextJobStatus.NEED_GENERATE)
    service = make_service(repository=repo, token_counter=SummaryTestCounter(), model=ContextSummaryModelRepository(SimpleNamespace()),
        prompt_resources=character_resources())
    service.generate_summary = AsyncMock(side_effect=InvalidStateError("缺少关键词"))
    from module.context_compaction.core.domain.errors import ContextGenerationRejected
    with pytest.raises(ContextGenerationRejected, match="缺少关键词"):
        await service.process_job("job")
    repo.save_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_validation_needs_no_model_and_keeps_candidate_unchanged():
    repo, started, source = process_materials(ContextJobStatus.NEED_VALIDATE)
    counter = SummaryTestCounter()
    service = make_service(repository=repo, token_counter=counter)
    candidate = service.build_candidate_summary(summary=StructuredSummary("candidate", keywords=("桌宠",)),
        manifest=source.source_manifest, attempt_version=3, candidate_version=1,
        versions=SummaryVersions(2, tokenizer_version=counter.tokenizer_version), token_counter=counter)
    repo.get_job.return_value = replace(started, status=ContextJobStatus.NEED_VALIDATE, candidate=candidate)
    repo.begin_validate.return_value = replace(repo.get_job.return_value, status=ContextJobStatus.VALIDATING, attempt_version=4)
    await service.process_job("job")
    job_id, version, validation = repo.save_validation.await_args.args
    assert job_id == "job" and version == 4 and validation.passed
    assert validation.candidate_hash == candidate.content_hash
    repo.begin_generate.assert_not_awaited()
    repo.publish_ready_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_publish_needs_no_model_or_token_counter():
    repo, _, _ = process_materials(ContextJobStatus.NEED_PUBLISH)
    await make_service(repository=repo).process_job("job")
    repo.publish_ready_snapshot.assert_awaited_once_with("job", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ContextJobStatus.GENERATING, ContextJobStatus.READY, ContextJobStatus.DONE,
    ContextJobStatus.FAILED, ContextJobStatus.OBSOLETE])
async def test_process_does_not_repeat_running_or_finished_tasks(status):
    repo, _, _ = process_materials(status)
    await make_service(repository=repo).process_job("job")
    repo.begin_generate.assert_not_awaited()
    repo.read_model_input.assert_not_awaited()
    repo.publish_ready_snapshot.assert_not_awaited()


@pytest.mark.parametrize("failure", ["count", "budget", "keywords", "dates", "outside_source"])
def test_program_validation_rejects_invalid_candidate(failure):
    from module.shared.context.types.references import ContextSourceRef
    service, counter = make_service(), SummaryTestCounter()
    manifest = base(4).source_manifest
    source = SummaryModelInput(plan([100] * 4), manifest, ())
    summary = StructuredSummary("history", keywords=("桌宠",))
    if failure == "budget":
        summary = replace(summary, summary="x" * 1000)
    elif failure == "keywords":
        summary = replace(summary, keywords=())
    elif failure == "dates":
        summary = replace(summary, commitments=(Commitment("c1", "user", "计划", 1, CommitmentStatus.ACTIVE, ()),))
    elif failure == "outside_source":
        summary = replace(summary, source_refs=(ContextSourceRef("outside", 1, "s"),))
    candidate = service.build_candidate_summary(summary=summary, manifest=manifest, attempt_version=1, candidate_version=1,
        versions=SummaryVersions(2, tokenizer_version=counter.tokenizer_version), token_counter=counter)
    if failure == "count":
        candidate = replace(candidate, token_count=0)
    assert not service.validate_candidate(source, candidate, counter).passed


def stats(counts):
    total, result = 0, []
    for seq, count in enumerate(counts, 1):
        total += count
        result.append(TurnTokenInfo(f"t{seq}", "s", seq, count, total, "deepseek-test"))
    return tuple(result)


def base(covered=10):
    manifest = SummarySourceManifest("s", TurnRange("s", 1, covered), (), "hash",
        None, False)
    return ContextSnapshot("snapshot", "s", SnapShotState.VALID, covered, StructuredSummary("history"), "history", 800, "hash", SummaryVersions(1, tokenizer_version="deepseek-test"), manifest, datetime.now(timezone.utc))


def plan(counts, **kwargs):
    options = dict(session_id="s", frozen_through_turn_seq=len(counts), turn_tokens=stats(counts),
                   prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy())
    options.update(kwargs)
    service = ContextCompactionService(
        prompt_budget=options.pop("prompt_budget"),
        compaction_policy=options.pop("compaction_policy"),
        lifecycle_policy=options.pop("lifecycle_policy", None),
    )
    return service.plan_compaction(**options)


@pytest.mark.parametrize("counts", [[100] * 30, [100] * 20, [2700], [3100]])
def test_every_handoff_covers_all_frozen_raw_without_retaining_rounds(counts):
    result = plan(counts)
    assert result.mode == CompactionMode.HANDOFF
    assert result.source_turns == TurnRange("s", 1, len(counts))
    assert result.retained_tail is None and result.context_only_turns is None
    assert result.planned_summary_tokens == 800


def test_incoming_reference_is_kept_exactly_without_expanding_ancestor_raw():
    incoming = InheritedContextRef.handoff("previous", 20, SnapshotRef("fixed", "hash", 17))
    result = plan([100] * 20, inherited_context=incoming)
    assert result.inherited_context == incoming
    assert result.source_turns == TurnRange("s", 1, 20)


def test_empty_source_has_no_handoff_work():
    assert plan([]) is None


@pytest.mark.parametrize("incoming", [
    InheritedContextRef("s", None, None, None),
    InheritedContextRef("previous", SnapshotRef("snap", "hash", 10), TurnRange("previous", 12, 15), None),
])
def test_inherited_source_must_be_a_distinct_continuous_predecessor(incoming):
    with pytest.raises(InvalidStateError):
        plan([100] * 20, inherited_context=incoming)


def test_prefix_length_and_cross_configuration_contracts():
    with pytest.raises(InvalidStateError):
        plan([100] * 20, turn_tokens=stats([100] * 19))
    with pytest.raises(InvalidStateError):
        plan([100] * 20, prompt_budget=replace(ContextPromptBudget(), history_budget=4000))
    with pytest.raises(InvalidStateError):
        plan([100] * 20, compaction_policy=replace(ContextCompactionPolicy(), summary_token_limit=3000))


def test_default_first_and_following_window_budgets():
    life = SessionLifecycleConfig()
    assert ContextPromptBudget().history_budget == life.session_target_tokens == 3000
    assert life.raw_token_budget(inherited_tokens=0) == 3000
    assert life.raw_token_budget(inherited_tokens=600) == 2400


def test_new_plan_and_source_manifest_roundtrip():
    result = plan([100] * 20)
    data = context_mappers.compaction_plan_to_json(result)
    assert "compress_range" not in data and "rebuild_source_ranges" not in data
    assert context_mappers.compaction_plan_from_json(data) == result
    manifest = base().source_manifest
    manifest_data = context_mappers.source_manifest_to_json(manifest)
    assert "rebuild_source_ranges" not in manifest_data
    assert context_mappers.source_manifest_from_json(manifest_data) == manifest
    assert context_mappers.compaction_plan_from_json(None) is None
    assert context_mappers.source_manifest_from_json(None) is None


@pytest.mark.parametrize("field", ["covered_through_turn_seq"])
@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_snapshot_rejects_invalid_coverage_and_depth_at_construction(field, value):
    with pytest.raises(ValueError, match=field):
        replace(base(), **{field: value})


def test_snapshot_rejects_manifest_from_another_session_at_construction():
    with pytest.raises(ValueError, match="source_manifest"):
        replace(base(), source_manifest=replace(base().source_manifest, session_id="other"))


@pytest.mark.parametrize("state", list(SnapShotState))
def test_snapshot_domain_allows_all_persisted_states(state):
    assert replace(base(), state=state).state == state




@pytest.mark.parametrize("tokens,retries,preserved,expected", [
    (800, 0, True, O.ACCEPT), (999, 0, True, O.ACCEPT),
    (1000, 0, True, O.RETRY), (1000, 1, True, O.REJECT),
    (1200, 1, True, O.REJECT), (800, 0, False, O.RETRY),
    (800, 1, False, O.REJECT), (0, 0, True, O.RETRY),
    (0, 1, True, O.REJECT),
])
def test_strict_summary_limit_and_review(tokens, retries, preserved, expected):
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy())
    result = service.assess_compaction(summary_tokens=tokens, retries_used=retries,
        information_preserved=preserved)
    assert result.outcome == expected
    assert result.summary_tokens == tokens
    assert result.target_met == (0 < tokens < 1000)


def test_retry_limit_is_configurable():
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(),
        compaction_policy=ContextCompactionPolicy(max_compression_retries=0))
    result = service.assess_compaction(summary_tokens=1000, retries_used=0, information_preserved=True)
    assert result.outcome == O.REJECT


@pytest.mark.parametrize("raw,incoming,ready,boundary,expected", [
    (0, 0, False, True, ()),
    (2399, 0, False, True, ()),
    (2400, 0, False, True, (A.PREPARE_HANDOFF,)),
    (3000, 0, False, True, (A.PREPARE_HANDOFF, A.WAIT_FOR_CONTEXT)),
    (1799, 600, False, True, ()),
    (1800, 600, False, True, (A.PREPARE_HANDOFF,)),
    (2249, 600, False, True, (A.PREPARE_HANDOFF,)),
    (2250, 600, False, True, (A.PREPARE_HANDOFF, A.WAIT_FOR_CONTEXT)),
    (2400, 600, False, True, (A.PREPARE_HANDOFF, A.WAIT_FOR_CONTEXT)),
    (1800, 600, True, True, (A.PREPARE_HANDOFF,)),
    (2250, 600, True, True, (A.ROTATE_WHEN_READY,)),
    (2400, 600, True, False, (A.PREPARE_HANDOFF, A.WAIT_FOR_CONTEXT)),
])
def test_window_lifecycle(raw, incoming, ready, boundary, expected):
    service = ContextCompactionService(prompt_budget=ContextPromptBudget(), compaction_policy=ContextCompactionPolicy())
    result = service.decide_context_maintenance(session_id="s", through_turn_seq=20,
        session_progress_tokens=raw, inherited_tokens=incoming,
        handoff_ready=ready, at_complete_turn_boundary=boundary)
    assert result.actions == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("generation_count,retryable", [(1, True), (2, False)])
async def test_invalid_output_uses_generation_budget_not_execution_version(generation_count, retryable):
    from module.context_compaction.core.domain.errors import ContextGenerationRejected
    repo, started, _ = process_materials(ContextJobStatus.NEED_GENERATE)
    repo.begin_generate.return_value = replace(started, attempt_count=generation_count, attempt_version=20)
    service = make_service(repository=repo, token_counter=SummaryTestCounter(), model=ContextSummaryModelRepository(SimpleNamespace()),
        prompt_resources=character_resources())
    service.generate_summary = AsyncMock(side_effect=InvalidStateError("缺少关键词"))
    with pytest.raises(ContextGenerationRejected) as caught:
        await service.process_job("job")
    assert caught.value.retryable is retryable
    repo.save_candidate.assert_not_awaited()
