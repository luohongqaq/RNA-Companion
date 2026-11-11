"""Topic生成边界：使用模拟模型结果，不请求远程服务。"""
from dataclasses import replace
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from module.shared.common.errors.base import InvalidStateError
from module.memory_management.shared.contracts.errors import MemoryModelUnavailable
from module.memory_management.topic.core.processing.prompts.generation import GENERATION_PROMPT
from module.memory_management.topic.core.processing.service import TopicProductionService
from module.memory_management.topic.core.domain.generation import TopicModelOutput
from module.memory_management.topic.core.domain.source import TopicSourceInput, TopicObjectGroup
from module.memory_management.object.core.domain.document import MemoryObject, ObjectEvent
from module.memory_management.topic.core.domain.plan import TopicPlan
from module.memory_management.topic.core.domain.source import TopicContext
from module.memory_management.topic.core.domain.document import TopicDocument, TopicIdentity, TopicCurrentState, TopicEntry
from module.shared.memory.types.references import EvidenceRef, MemoryRevisionRef, MemoryKind
from module.memory_management.topic.repository import mappers as topic_mappers
from module.memory_management.topic.repository.llm import TopicModelRepository


def source_input(with_topic=False):
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    objects = []
    for cid, sid, mid in (("h1", "s1", "m1"), ("h2", "s2", "m2"), ("new", "s3", "m3")):
        ref = EvidenceRef(sid, mid, f"原始引用-{cid}", f"turn-{cid}")
        objects.append(MemoryObject(cid, "c", "具体项目",
            (ObjectEvent("user", f"阶段-{cid}", now.date(), None, (ref,)),), f"{sid}:{cid}", now))
    contexts = ()
    if with_topic:
        ref = EvidenceRef("s0", "m0", "既有原始引用")
        document = TopicDocument("具体项目", TopicIdentity("同一个具体项目", (ref,)),
            TopicCurrentState("原有进展", ("old",)),
            (TopicEntry("old", "原有进展", None, now.date(), (ref,)),))
        contexts = (TopicContext(MemoryRevisionRef(MemoryKind.TOPIC, "existing", 2, document.content_hash()), document),)
    plan = TopicPlan("c", ("new",), ("h1", "h2"), tuple(item.ref for item in contexts))
    return TopicSourceInput(plan, (TopicObjectGroup(objects[2], tuple(objects[:2]), contexts),))


def ref(sid, mid):
    return {"session_id": sid, "message_id": mid}


def windowed_source():
    """基础topic含一条窗口外旧条目(10-01)和一条窗口内条目(10-25)；本批anchor为11-01。"""
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    base_ref = EvidenceRef("s0", "m0", "原始引用")
    document = TopicDocument("具体项目", TopicIdentity("同一个具体项目", (base_ref,)),
        TopicCurrentState("原有进展", ("recent",)),
        (TopicEntry("old", "原有进展", None, date(2026, 10, 1), (base_ref,)),
         TopicEntry("recent", "近期进展", None, date(2026, 10, 25), (base_ref,))))
    context = TopicContext(MemoryRevisionRef(MemoryKind.TOPIC, "existing", 2, document.content_hash()), document)
    new_ref = EvidenceRef("s3", "m3", "新引用")
    candidate = MemoryObject("new", "c", "具体项目",
        (ObjectEvent("user", "新阶段", now.date(), None, (new_ref,)),), "s3:new", now)
    plan = TopicPlan("c", ("new",), (), (context.ref,))
    return TopicSourceInput(plan, (TopicObjectGroup(candidate, (), (context,)),))


def test_payload_only_exposes_operable_entries():
    source = windowed_source()
    related = source.to_model_payload()["GROUPS"][0]["RELATED_TOPICS"][0]
    assert [row["entry_id"] for row in related["document"]["entries"]] == ["recent"]
    assert "timeline_window" not in related
    assert "hidden_entry_ids" not in json.dumps(related, default=str)


def pruned_source():
    """基础topic含四种计划：过期(10-20说、目标10-21)、长期(无目标日)、未到期(目标11-15)、当日(11-01说)；anchor=11-01。"""
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    base_ref = EvidenceRef("s0", "m0", "原始引用")

    def plan(eid, statement, acquire, target):
        return TopicEntry(eid, statement, None, acquire, (base_ref,), kind="plan", actor="user", target_time=target)

    events = (TopicEntry("recent", "近期进展", None, date(2026, 10, 25), (base_ref,)),
              plan("expired", "过期计划", date(2026, 10, 20), date(2026, 10, 21)),
              plan("longterm", "长期计划", date(2026, 10, 20), None),
              plan("upcoming", "未到期计划", date(2026, 10, 20), date(2026, 11, 15)),
              plan("today", "当日计划", date(2026, 11, 1), date(2026, 10, 31)))
    document = TopicDocument("具体项目", TopicIdentity("同一个具体项目", (base_ref,)),
        TopicCurrentState("原有进展", ("recent", "expired")), events)
    context = TopicContext(MemoryRevisionRef(MemoryKind.TOPIC, "existing", 2, document.content_hash()), document)
    new_ref = EvidenceRef("s3", "m3", "新引用")
    candidate = MemoryObject("new", "c", "具体项目",
        (ObjectEvent("user", "新阶段", now.date(), None, (new_ref,)),), "s3:new", now)
    return TopicSourceInput(TopicPlan("c", ("new",), (), (context.ref,)),
                            (TopicObjectGroup(candidate, (), (context,)),))


def _prune_model_data(include_expired):
    rows = [event("recent", "s0", "m0", "近期进展")]
    rows[0]["acquire_time"] = "2026-10-25"
    if include_expired:
        rows.append(dict(entry_id="expired", statement="过期计划", kind="plan", actor="user",
            acquire_time="2026-10-20", occurrence_time=None, target_time="2026-10-21",
            source_refs=[ref("s0", "m0")], corrects_entry_id=None))
    rows += [
        dict(entry_id="longterm", statement="长期计划", kind="plan", actor="user",
             acquire_time="2026-10-20", occurrence_time=None, target_time=None,
             source_refs=[ref("s0", "m0")], corrects_entry_id=None),
        dict(entry_id="upcoming", statement="未到期计划", kind="plan", actor="user",
             acquire_time="2026-10-20", occurrence_time=None, target_time="2026-11-15",
             source_refs=[ref("s0", "m0")], corrects_entry_id=None),
        dict(entry_id="today", statement="当日计划", kind="plan", actor="user",
             acquire_time="2026-11-01", occurrence_time=None, target_time="2026-10-31",
             source_refs=[ref("s0", "m0")], corrects_entry_id=None),
        event("new-entry", "s3", "m3", "新进展"),
    ]
    return {"applications": [dict(
        action="update", target_topic_id="existing", object_ids=["new"],
        document=dict(topic="具体项目",
            topic_identity=dict(description="同一个具体项目", source_refs=[ref("s0", "m0")]),
            current_state=dict(text="最新状态", source_entry_ids=["recent"]),
            entries=rows),
        operations=[dict(kind="add_event", object_ids=["new"], source_refs=[ref("s3", "m3")],
            target_entry_id=None, proposed_entry_id="new-entry")],
        discussion_source_refs=[], reason="同一主题的实质进展。")], "pending_object_ids": []}


def test_update_prunes_expired_plans():
    application = TopicModelOutput.from_model_data(
        _prune_model_data(False), source=pruned_source(), prompt=GENERATION_PROMPT).result.applications[0]
    ids = [item.entry_id for item in application.document.entries]
    # 过期计划无论模型是否回显都被移除；长期、未到期、当日计划保留
    assert ids == ["longterm", "upcoming", "recent", "today", "new-entry"]
    # current_state 对已裁剪计划的引用被程序过滤
    assert application.document.current_state.source_entry_ids == ("recent",)


def test_update_restores_hidden_entries_outside_window():
    data = {"applications": [dict(
        action="update", target_topic_id="existing", object_ids=["new"],
        document=dict(topic="具体项目",
            topic_identity=dict(description="同一个具体项目", source_refs=[ref("s0", "m0")]),
            current_state=dict(text="最新状态", source_entry_ids=["recent", "new-entry"]),
            entries=[event("recent", "s0", "m0", "近期进展"), event("new-entry", "s3", "m3", "新进展")]),
        operations=[dict(kind="add_event", object_ids=["new"], source_refs=[ref("s3", "m3")],
            target_entry_id=None, proposed_entry_id="new-entry")],
        discussion_source_refs=[], reason="同一主题的实质进展。")], "pending_object_ids": []}
    data["applications"][0]["document"]["entries"][0]["acquire_time"] = "2026-10-25"
    application = TopicModelOutput.from_model_data(data, source=windowed_source(), prompt=GENERATION_PROMPT).result.applications[0]
    # 模型只回显了窗口内条目；窗口外的old由程序拼回，顺序按获知日稳定排列
    assert [item.entry_id for item in application.document.entries] == ["old", "recent", "new-entry"]
    assert application.document.entries[0].statement == "原有进展"


def event(entry_id, sid, mid, statement):
    return dict(entry_id=entry_id, statement=statement, kind="event", actor="user",
                acquire_time="2026-11-01", occurrence_time=None, target_time=None,
                source_refs=[ref(sid, mid)], corrects_entry_id=None)


def model_data(update=False):
    if update:
        rows = [event("old", "s0", "m0", "原有进展"), event("new-entry", "s3", "m3", "新进展")]
        identity_refs = [ref("s0", "m0")]
        ids = ["new"]
        substantive = []
    else:
        rows = [event(f"e{i}", f"s{i}", f"m{i}", f"阶段{i}") for i in range(1, 4)]
        identity_refs = [ref("s1", "m1")]
        ids = ["h1", "h2", "new"]
        substantive = [ref(f"s{i}", f"m{i}") for i in range(1, 4)]
    operations = []
    for cid, row in zip(["new"] if update else ids, rows[1:] if update else rows, strict=True):
        operations.append(dict(kind="add_event", object_ids=[cid], source_refs=row["source_refs"],
                               target_entry_id=None, proposed_entry_id=row["entry_id"]))
    return dict(applications=[dict(
        action="update" if update else "create", target_topic_id="existing" if update else None,
        object_ids=ids, document=dict(topic="具体项目",
            topic_identity=dict(description="同一个具体项目", source_refs=identity_refs),
            current_state=dict(text=rows[-1]["statement"], source_entry_ids=[rows[-1]["entry_id"]]), entries=rows),
        operations=operations, discussion_source_refs=substantive, reason="同一事件主题的实质进展。")], pending_object_ids=[])


def test_create_counts_evidenced_sessions_and_program_assigns_identity():
    source = source_input()
    output = TopicModelOutput.from_model_data(model_data(), source=source, prompt=GENERATION_PROMPT)
    candidate = output.result
    app = candidate.applications[0]
    assert app.discussion_session_ids == ("s1", "s2", "s3")  # 同日三个session仍然达标
    assert app.topic_id and app.application_id and app.base_ref is None
    assert app.document.entries[2].source_refs[0].quote == "原始引用-new"
    assert app.document.entries[2].source_refs[0].turn_id == "turn-new"
    assert len(app.discussion_source_refs) == 3 and app.reason == "同一事件主题的实质进展。"
    again = TopicModelOutput.from_model_data(model_data(), source=source, prompt=GENERATION_PROMPT).result
    assert again.applications[0].topic_id == app.topic_id
    assert again.content_hash == candidate.content_hash
    restored = topic_mappers._memory_topic_batch_result_from_json(json.loads(json.dumps(topic_mappers._memory_topic_batch_result_to_json(candidate))))
    assert restored == candidate


def test_update_uses_fixed_version_and_preserves_old_events_without_new_threshold():
    source = source_input(True)
    app = TopicModelOutput.from_model_data(model_data(True), source=source, prompt=GENERATION_PROMPT).result.applications[0]
    assert app.topic_id == "existing" and app.base_ref == source.plan.topic_refs[0]
    assert app.document.entries[0] == source.groups[0].existing_topics[0].document.entries[0]
    assert app.discussion_session_ids == ()


@pytest.mark.parametrize("case", ["foreign_object", "invented_ref", "missing_refs", "too_few_sessions", "missing_new_object"])
def test_create_rejects_invalid_model_scope(case):
    data = model_data()
    if case == "foreign_object":
        data["applications"][0]["object_ids"].append("foreign")
    elif case == "invented_ref":
        data["applications"][0]["document"]["entries"][0]["source_refs"] = [ref("s1", "missing")]
    elif case == "missing_refs":
        data["applications"][0]["document"]["entries"][0]["source_refs"] = []
    elif case == "too_few_sessions":
        data["applications"][0]["discussion_source_refs"].pop()
    else:
        data = dict(applications=[], pending_object_ids=[])
    with pytest.raises(InvalidStateError):
        TopicModelOutput.from_model_data(data, source=source_input(), prompt=GENERATION_PROMPT)


@pytest.mark.parametrize("case", ["renamed_topic", "invented_progress_from_old_ref"])
def test_update_rejects_invalid_document_changes(case):
    data = model_data(True)
    document = data["applications"][0]["document"]
    if case == "renamed_topic":
        document["topic"] = "另一个主题"
    else:
        document["entries"][1]["source_refs"] = [ref("s0", "m0")]
    with pytest.raises(InvalidStateError):
        TopicModelOutput.from_model_data(data, source=source_input(True), prompt=GENERATION_PROMPT)


def test_update_allows_identity_evolution_and_keeps_name():
    data = model_data(True)
    document = data["applications"][0]["document"]
    document["topic_identity"]["description"] = "扩展后涵盖记忆网络调试的项目"
    document["topic_identity"]["source_refs"].append(ref("s3", "m3"))
    output = TopicModelOutput.from_model_data(data, source=source_input(True), prompt=GENERATION_PROMPT)
    application = output.result.applications[0]
    assert application.document.topic_identity.description == "扩展后涵盖记忆网络调试的项目"


def source_with_document(source, document):
    """模拟用户已保存的新正式版本；后续解析只能看到这个版本。"""
    previous = source.groups[0].existing_topics[0]
    context = TopicContext(replace(previous.ref, revision=previous.ref.revision + 1,
                                   content_hash=document.content_hash()), document)
    return TopicSourceInput(replace(source.plan, topic_refs=(context.ref,)),
        (replace(source.groups[0], existing_topics=(context,)),))


def user_edited_source():
    source = source_input(True)
    document = source.groups[0].existing_topics[0].document
    edited = replace(document, topic="用户改过的项目名",
        topic_identity=TopicIdentity("用户写的主题说明", ()),
        entries=(replace(document.entries[0], statement="用户手动记录的进展", source_refs=()),))
    return source_with_document(source, edited)


def update_from_current_document(source):
    data = model_data(True)
    application = data["applications"][0]
    document = source.groups[0].existing_topics[0].document.to_data()
    for item in (document["topic_identity"], *document["entries"]):
        item["source_refs"] = [ref(row["session_id"], row["message_id"]) for row in item["source_refs"]]
    document["entries"].append(event("new-entry", "s3", "m3", "新进展"))
    document["current_state"] = dict(text="新进展", source_entry_ids=["new-entry"])
    application["document"] = document
    return data


def test_update_preserves_user_content_and_empty_sources():
    source = user_edited_source()
    application = TopicModelOutput.from_model_data(update_from_current_document(source),
        source=source, prompt=GENERATION_PROMPT).result.applications[0]
    base = source.groups[0].existing_topics[0]
    assert application.base_ref == base.ref
    assert application.document.topic == base.document.topic
    assert application.document.topic_identity == base.document.topic_identity
    assert application.document.entries[0] == base.document.entries[0]
    assert application.document.entries[1].source_refs[0].quote == "原始引用-new"


@pytest.mark.parametrize("field,value", [
    ("statement", "AI改写了用户的文字"), ("acquire_time", "2026-10-31"),
    ("occurrence_time", "2026-10-30"), ("kind", "plan"), ("actor", "assistant"),
    ("corrects_entry_id", "new-entry"), ("target_time", "2026-11-10"),
])
def test_reusing_user_entry_id_does_not_exempt_changed_content(field, value):
    source = user_edited_source()
    if field == "target_time":
        base = source.groups[0].existing_topics[0].document
        source = source_with_document(source, replace(base,
            entries=(replace(base.entries[0], kind="plan", target_time=date(2026, 11, 9)),)))
    data = update_from_current_document(source)
    data["applications"][0]["document"]["entries"][0][field] = value
    with pytest.raises(InvalidStateError, match="修改旧条目必须引用|更正只能指向"):
        TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT)


@pytest.mark.parametrize("user_content", [True, False])
def test_ai_can_update_existing_content_with_selected_object_evidence(user_content):
    source = user_edited_source() if user_content else source_input(True)
    data = update_from_current_document(source)
    application = data["applications"][0]
    entry = application["document"]["entries"][0]
    entry["statement"] = "根据新的进度更新用户内容"
    entry["source_refs"].append(ref("s3", "m3"))
    application["operations"].append(dict(kind="revise_event", object_ids=["new"],
        source_refs=[ref("s3", "m3")], target_entry_id="old", proposed_entry_id="old"))
    result = TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT).result
    updated = result.applications[0].document.entries[0]
    assert updated.statement == entry["statement"]
    assert updated.source_refs[-1].quote == "原始引用-new"
    assert len(updated.source_refs) == (1 if user_content else 2)


def test_ai_cannot_change_old_content_using_only_old_evidence():
    source = source_input(True)
    data = update_from_current_document(source)
    data["applications"][0]["document"]["entries"][0]["statement"] = "只有旧来源的改写"
    with pytest.raises(InvalidStateError, match="修改旧条目必须引用"):
        TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT)


def test_ai_cannot_change_identity_using_only_old_evidence():
    source = source_input(True)
    data = update_from_current_document(source)
    data["applications"][0]["document"]["topic_identity"]["description"] = "只有旧来源的主题扩展"
    with pytest.raises(InvalidStateError, match="修改主题说明必须引用"):
        TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT)


@pytest.mark.parametrize("refs,accepted", [([], False), ([ref("s3", "m3")], True)])
def test_user_identity_changes_need_selected_object_evidence(refs, accepted):
    source = user_edited_source()
    data = update_from_current_document(source)
    identity = data["applications"][0]["document"]["topic_identity"]
    identity.update(description="AI扩展了用户的主题说明", source_refs=refs)
    if accepted:
        result = TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT).result
        assert result.applications[0].document.topic_identity.description == identity["description"]
    else:
        with pytest.raises(InvalidStateError, match="修改主题说明必须引用"):
            TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT)


@pytest.mark.parametrize("section", ["entry", "identity"])
def test_unchanged_evidenced_content_cannot_drop_its_sources(section):
    source = source_input(True)
    data = update_from_current_document(source)
    document = data["applications"][0]["document"]
    item = document["entries"][0] if section == "entry" else document["topic_identity"]
    item["source_refs"] = []
    with pytest.raises(InvalidStateError, match="不能清空已有来源"):
        TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT)


def test_create_still_requires_identity_evidence():
    data = model_data()
    data["applications"][0]["document"]["topic_identity"]["source_refs"] = []
    with pytest.raises(InvalidStateError, match="主题说明必须引用"):
        TopicModelOutput.from_model_data(data, source=source_input(), prompt=GENERATION_PROMPT)


def test_new_entries_in_user_document_still_require_selected_object_sources():
    source = user_edited_source()
    data = update_from_current_document(source)
    data["applications"][0]["document"]["entries"][-1]["source_refs"] = []
    with pytest.raises(InvalidStateError, match="新增时间线条目必须引用"):
        TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT)


@pytest.mark.parametrize("delete_all", [False, True])
def test_deleted_user_entries_are_not_restored_from_previous_versions(delete_all):
    source = windowed_source()
    previous = source.groups[0].existing_topics[0].document
    retained = () if delete_all else (replace(previous.entries[1], source_refs=()),)
    document = replace(previous, topic_identity=replace(previous.topic_identity, source_refs=()),
        entries=retained, current_state=TopicCurrentState("用户删除后的状态", tuple(e.entry_id for e in retained)))
    source = source_with_document(source, document)
    payload = source.to_model_payload()["GROUPS"][0]["RELATED_TOPICS"][0]
    assert "timeline_window" not in payload
    result = TopicModelOutput.from_model_data(update_from_current_document(source),
        source=source, prompt=GENERATION_PROMPT).result
    entries = result.applications[0].document.entries
    assert [e.entry_id for e in entries] == (["new-entry"] if delete_all else ["recent", "new-entry"])


def test_hidden_user_entry_with_empty_sources_is_restored_unchanged():
    source = windowed_source()
    base = source.groups[0].existing_topics[0].document
    edited = replace(base, topic_identity=replace(base.topic_identity, source_refs=()),
                     entries=tuple(replace(e, source_refs=()) for e in base.entries))
    source = source_with_document(source, edited)
    data = update_from_current_document(source)
    data["applications"][0]["document"]["entries"].pop(0)
    application = TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT).result.applications[0]
    assert application.document.entries[0] == edited.entries[0]


def delta_update(source):
    data = update_from_current_document(source)
    document = data["applications"][0]["document"]
    document["entries"] = document["entries"][-1:]
    document["topic_identity"] = None
    return data


def test_delta_preserves_unsubmitted_user_entries_and_identity():
    source = user_edited_source()
    application = TopicModelOutput.from_model_data(delta_update(source),
        source=source, prompt=GENERATION_PROMPT).result.applications[0]
    base = source.groups[0].existing_topics[0].document
    assert application.document.entries[0] == base.entries[0]
    assert application.document.topic_identity == base.topic_identity
    assert [entry.entry_id for entry in application.document.entries] == ["old", "new-entry"]


def test_delta_state_can_reference_unsubmitted_visible_entry():
    source = user_edited_source()
    data = delta_update(source)
    data["applications"][0]["document"]["current_state"] = dict(text="原进展仍然有效", source_entry_ids=["old"])
    application = TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT).result.applications[0]
    assert application.document.current_state.source_entry_ids == ("old",)


def test_omitted_sections_preserve_even_state_references_outside_window():
    source = windowed_source()
    base = source.groups[0].existing_topics[0].document
    source = source_with_document(source, replace(base, current_state=TopicCurrentState("此前状态", ("old",))))
    payload = source.to_model_payload()["GROUPS"][0]["RELATED_TOPICS"][0]
    assert payload["document"]["current_state"]["source_entry_ids"] == ()
    assert '"old"' not in json.dumps(payload, default=str)
    data = delta_update(source)
    data["applications"][0]["document"]["current_state"] = None
    application = TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT).result.applications[0]
    assert application.document.current_state.source_entry_ids == ("old",)
    assert [entry.entry_id for entry in application.document.entries] == ["old", "recent", "new-entry"]


def test_input_prunes_plans_before_model_sees_them():
    payload = pruned_source().to_model_payload()["GROUPS"][0]["RELATED_TOPICS"][0]
    assert {row["entry_id"] for row in payload["document"]["entries"]} == {"recent", "longterm", "upcoming", "today"}
    assert payload["document"]["current_state"]["source_entry_ids"] == ("recent",)
    assert "timeline_window" not in payload


def test_merge_evidence_adds_sources_without_repeating_target_body():
    source = user_edited_source()
    data = delta_update(source)
    application = data["applications"][0]
    application["document"].update(entries=[], current_state=None)
    application["operations"] = [dict(kind="merge_evidence", object_ids=["new"],
        source_refs=[ref("s3", "m3")], target_entry_id="old", proposed_entry_id=None)]
    output = TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT).result
    base = source.groups[0].existing_topics[0].document
    updated = output.applications[0].document
    assert updated.entries[0].statement == base.entries[0].statement
    assert updated.entries[0].source_refs[0].quote == "原始引用-new"
    assert updated.current_state == base.current_state
    assert len(updated.entries) == 1


def test_correction_only_submits_new_entry_and_keeps_visible_target():
    source = user_edited_source()
    data = delta_update(source)
    application = data["applications"][0]
    application["document"]["entries"][0]["corrects_entry_id"] = "old"
    application["operations"][0].update(kind="correct_event", target_entry_id="old")
    output = TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT).result.applications[0]
    assert output.document.entries[0] == source.groups[0].existing_topics[0].document.entries[0]
    assert output.document.entries[1].corrects_entry_id == "old"


@pytest.mark.parametrize("kind", ["merge_evidence", "revise_event", "correct_event"])
def test_operations_cannot_target_unshown_entry(kind):
    source = windowed_source()
    data = delta_update(source)
    application = data["applications"][0]
    operation = application["operations"][0]
    operation.update(kind=kind, target_entry_id="old")
    if kind == "merge_evidence":
        operation["proposed_entry_id"] = None
        application["document"].update(entries=[], current_state=None)
    elif kind == "revise_event":
        operation["proposed_entry_id"] = "old"
        application["document"]["entries"][0]["entry_id"] = "old"
    else:
        application["document"]["entries"][0]["corrects_entry_id"] = "old"
    with pytest.raises(InvalidStateError, match="本次展示"):
        TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT)


def test_state_cannot_guess_unshown_entry_id():
    source = windowed_source()
    data = delta_update(source)
    data["applications"][0]["document"]["current_state"]["source_entry_ids"] = ["old"]
    with pytest.raises(InvalidStateError, match="当前状态只能引用"):
        TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT)


@pytest.mark.parametrize("field", ["topic_identity", "current_state"])
def test_create_cannot_omit_sections(field):
    data = model_data()
    data["applications"][0]["document"][field] = None
    with pytest.raises(InvalidStateError, match="新建topic必须提供"):
        TopicModelOutput.from_model_data(data, source=source_input(), prompt=GENERATION_PROMPT)


def test_change_without_operation_is_rejected():
    source = user_edited_source()
    data = delta_update(source)
    data["applications"][0]["operations"] = []
    with pytest.raises(InvalidStateError, match="必须声明对应操作"):
        TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT)


def test_duplicate_submitted_ids_are_not_silently_overwritten():
    source = user_edited_source()
    data = delta_update(source)
    rows = data["applications"][0]["document"]["entries"]
    rows.append(dict(rows[0]))
    with pytest.raises(InvalidStateError, match="条目ID不能重复"):
        TopicModelOutput.from_model_data(data, source=source, prompt=GENERATION_PROMPT)


def test_prompt_contains_only_current_task_contract():
    for text in ("hidden_entry_ids", "timeline_window", "用户编辑", "用户已从", "过期计划", "document.events"):
        assert text not in GENERATION_PROMPT
    assert "仅列新增、修订后的条目" in GENERATION_PROMPT


def test_misnested_operations_returns_actionable_error():
    data = model_data()
    application = data["applications"][0]
    application["document"]["operations"] = application.pop("operations")
    with pytest.raises(InvalidStateError, match="operations必须与document同级"):
        TopicModelOutput.from_model_data(data, source=source_input(), prompt=GENERATION_PROMPT)


@pytest.mark.asyncio
async def test_generate_topics_uses_prompt_json_structure_and_returns_candidate():
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(model_data())))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response))))
    service = TopicProductionService(None, model=TopicModelRepository(client))
    result = await service.generate_topics(source_input(), feedback="上次来源引用有误")
    request = client.chat.completions.create.call_args.kwargs
    assert request["messages"][0]["content"] == GENERATION_PROMPT
    assert request["reasoning_effort"] == "high"
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert request["response_format"] == {"type": "json_object"}
    assert request["max_tokens"] == service._model_config.max_output_tokens
    payload = json.loads(request["messages"][1]["content"])
    assert payload["GROUPS"][0]["OBJECT"]["object_id"] == "new"
    assert payload["PREVIOUS_ERRORS"] == "上次来源引用有误"
    assert "SOURCE_SESSIONS" not in payload
    assert len(result.applications) == 1
    client.chat.completions.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_topics_rejects_incomplete_and_retryable_transport_error():
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(
        return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content="{}"))])))))
    service = TopicProductionService(None, model=TopicModelRepository(client))
    with pytest.raises(MemoryModelUnavailable) as error:
        await service.generate_topics(source_input())
    assert error.value.retryable
    client.chat.completions.create.side_effect = ConnectionError("connection dropped")
    with pytest.raises(MemoryModelUnavailable) as error:
        await service.generate_topics(source_input())
    assert error.value.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "", "   \n ", "{", "{}\n{}", "[]"])
async def test_generate_topics_rejects_blank_or_invalid_json_after_normal_stop(content):
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
        message=SimpleNamespace(content=content))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response))))
    with pytest.raises(MemoryModelUnavailable) as error:
        await TopicProductionService(None, model=TopicModelRepository(client)).generate_topics(source_input())
    assert error.value.retryable
    client.chat.completions.create.assert_awaited_once()
