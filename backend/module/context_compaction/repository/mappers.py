"""正式快照、压缩任务和交接材料的 SQLite 映射。"""

from datetime import date
from typing import Any
from module.context_compaction.core.domain.plan import TurnTokenInfo
from module.shared.context.types.references import ContextSourceRef, SnapshotRef
from module.shared.context.types.summary import (
    Commitment,
    CommitmentStatus,
    LatestTopic,
    ObjectBinding,
    SceneState,
    StructuredSummary,
    ToneContext,
    UnresolvedQuestion,
)
from module.shared.context.types.working import SnapshotContext
from storage.sqlite.models import ContextSnapshotRow, TurnRow, ContextJobRow
from module.context_compaction.core.domain.policy import ContextCompactionPolicy
from module.chat.reply.core.domain.config.context_budget import ContextPromptBudget
from module.context_compaction.core.domain.job import ContextJob, ContextJobStatus, ContextJobType
from module.context_compaction.core.domain.plan import CompactionPlan
from module.context_compaction.core.domain.references import CompactionMode, InheritedSessionRangeRef
from module.context_compaction.core.domain.handoff import HandoffMode, HandoffPacket
from module.shared.context.types.summary import SummaryVersions
from module.context_compaction.core.domain.snapshots import (
    ContextSnapshot,
    SnapShotState,
    SummarySourceManifest,
)
from module.context_compaction.core.domain.candidate import SummaryCandidate, SummaryValidation
from storage.sqlite.value_codecs import SQLiteValueCodec


def snapshot_to_ref(row: ContextSnapshotRow) -> SnapshotRef:
    """T2-A固定快照引用；正文由事务外上下文加载器按引用读取。"""
    return SnapshotRef(snapshot_id=row.snapshot_id,
                       content_hash=row.content_hash,
                       covered_through_turn_seq=row.covered_through_turn_seq)


def snapshot_to_context(row: ContextSnapshotRow) -> SnapshotContext:
    """读取当前结构的摘要正文，不对缺字段的旧JSON补默认值。"""
    return SnapshotContext(
        ref=snapshot_to_ref(row),
        structured_summary=structured_summary_from_json(row.summary_json),
        summary_text=row.rendered_summary_text,
    )


def snapshot_to_row(value: ContextSnapshot, *, producer_job_id: str | None = None) -> ContextSnapshotRow:
    """创建完整快照行；生产任务标识由调用方提供，不隐式改变有效状态。"""
    return ContextSnapshotRow(
        snapshot_id=value.snapshot_id, session_id=value.session_id,
        covered_through_turn_seq=value.covered_through_turn_seq, snapshot_state=value.state.value,
        summary_json=structured_summary_to_json(value.summary), rendered_summary_text=value.summary_text,
        rendered_token_count=value.token_count, content_hash=value.content_hash,
        versions_json=_summary_versions_to_json(value.versions),
        source_manifest_json=source_manifest_to_json(value.source_manifest),
        created_at=value.created_at, producer_job_id=producer_job_id,
    )


def snapshot_to_domain(row: ContextSnapshotRow) -> ContextSnapshot:
    """还原session唯一摘要及完整来源清单，不补造缺失的审计数据。"""
    if row.source_manifest_json is None:
        raise ValueError("snapshot is missing its source manifest")
    return ContextSnapshot(
        snapshot_id=row.snapshot_id, session_id=row.session_id,
        state=SnapShotState(row.snapshot_state), covered_through_turn_seq=row.covered_through_turn_seq,
        summary=structured_summary_from_json(row.summary_json), summary_text=row.rendered_summary_text,
        token_count=row.rendered_token_count, content_hash=row.content_hash,
        versions=_summary_versions_from_json(row.versions_json),
        source_manifest=source_manifest_from_json(row.source_manifest_json), created_at=row.created_at,
    )


def context_job_to_row(value: ContextJob) -> ContextJobRow:
    """创建上下文任务行；已有任务更新时逐项修改已加载的 ORM 行。"""
    return ContextJobRow(
        job_id=value.job_id, job_type=value.job_type.value, session_id=value.session_id,
        target_key=value.target_key, dedupe_key=value.dedupe_key, priority=value.priority,
        status=value.status.value, retry_count=value.retry_count, available_at=value.available_at,
        created_at=value.created_at, updated_at=value.updated_at,
        payload_json=context_job_payload_to_json(value),
    )


def context_job_to_domain(row: ContextJobRow) -> ContextJob:
    """把上下文任务行还原成业务对象，不读取数据库或推进任务状态。

    普通列提供任务身份和调度状态；payload_json 提供来源上界及阶段产物。
    当前协议允许字段值为 None，但缺少字段明确报错，不静默补齐半份任务。
    """
    data = row.payload_json
    if not isinstance(data, dict):
        raise ValueError("context job payload must be a JSON object")

    # v4仅保存生成候选与程序校验，不兼容旧审核协议。
    schema_version = data["schema_version"]
    if type(schema_version) is not int or schema_version != 4:
        raise ValueError("unsupported context job payload schema")

    return ContextJob(
        # 任务身份和调度状态来自独立数据库列。
        job_id=row.job_id,
        job_type=ContextJobType(row.job_type),
        session_id=row.session_id,
        target_key=row.target_key,
        dedupe_key=row.dedupe_key,
        priority=row.priority,
        status=ContextJobStatus(row.status),
        available_at=row.available_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        retry_count=row.retry_count,
        # 固定计划和来源：嵌套 JSON 还原成相应数据类。
        frozen_through_turn_seq=data["frozen_through_turn_seq"],
        plan=compaction_plan_from_json(data["plan"]),
        source_manifest=source_manifest_from_json(data["source_manifest"]),
        # 候选只有一份，校验只保存报告，不改写正文。
        candidate=summary_candidate_from_json(data["candidate"]),
        validation=validation_from_json(data["validation"]),
        # 发布结果与交接材料只还原引用和数据，不在 mapper 中采用摘要。
        handoff_packet=handoff_packet_from_json(data["handoff_packet"]),
        published_snapshot=SQLiteValueCodec.snapshot_from_json(data["published_snapshot"]),
        # 尝试代次用于拒绝迟到结果，finished_at 从 JSON 时间还原为 UTC。
        attempt_version=data["attempt_version"],
        attempt_count=data["attempt_count"],
        last_error=data["last_error"],
        finished_at=(
            SQLiteValueCodec.datetime_from_json(data["finished_at"])
            if data["finished_at"] is not None
            else None
        ),
    )


def context_job_payload_to_json(value: ContextJob) -> dict[str, Any]:
    """任务阶段产物转为带版本号的 payload；不修改 ORM 对象。"""
    return {
        "schema_version": 4,
        "frozen_through_turn_seq": value.frozen_through_turn_seq,
        "plan": compaction_plan_to_json(value.plan) if value.plan is not None else None,
        "candidate": summary_candidate_to_json(value.candidate) if value.candidate is not None else None,
        "validation": validation_to_json(value.validation) if value.validation is not None else None,
        "handoff_packet": handoff_packet_to_json(value.handoff_packet) if value.handoff_packet is not None else None,
        "published_snapshot": SQLiteValueCodec.snapshot_to_json(value.published_snapshot) if value.published_snapshot is not None else None,
        "last_error": value.last_error if value.last_error is not None else None,
        "finished_at": SQLiteValueCodec.datetime_to_json(value.finished_at) if value.finished_at is not None else None,
        "attempt_version": value.attempt_version,
        "attempt_count": value.attempt_count,
        "source_manifest": source_manifest_to_json(value.source_manifest) if value.source_manifest is not None else None,
    }


def _inherited_range_to_json(value: InheritedSessionRangeRef | None) -> dict[str, Any] | None:
    """将 InheritedSessionRangeRef 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "session_id": value.session_id,
        "from_turn_seq": value.from_turn_seq,
        "through_turn_seq": value.through_turn_seq,
        "source_hash": value.source_hash,
        "source_refs": [_source_ref_to_json(item0) for item0 in value.source_refs],
    }


def _inherited_range_from_json(data: dict[str, Any] | None) -> InheritedSessionRangeRef | None:
    """将 JSON 逐字段还原为 InheritedSessionRangeRef；缺少字段明确报错。"""
    if data is None:
        return None
    return InheritedSessionRangeRef(
        session_id=data["session_id"],
        from_turn_seq=data["from_turn_seq"],
        through_turn_seq=data["through_turn_seq"],
        source_hash=data["source_hash"],
        source_refs=tuple(_source_ref_from_json(item0) for item0 in data["source_refs"]),
    )


def _source_ref_to_json(value: ContextSourceRef | None) -> dict[str, Any] | None:
    """将 ContextSourceRef 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "turn_id": value.turn_id,
        "revision": value.revision,
        "session_id": value.session_id if value.session_id is not None else None,
    }


def _source_ref_from_json(data: dict[str, Any] | None) -> ContextSourceRef | None:
    """将 JSON 逐字段还原为 ContextSourceRef；缺少字段明确报错。"""
    if data is None:
        return None
    return ContextSourceRef(
        turn_id=data["turn_id"],
        revision=data["revision"],
        session_id=data["session_id"] if data["session_id"] is not None else None,
    )


def _source_refs_to_json(
    values: tuple[ContextSourceRef, ...],
) -> list[dict[str, Any]]:
    """把摘要来源引用元组转成 JSON 数组。"""
    return [
        {
            "turn_id": value.turn_id,
            "revision": value.revision,
            "session_id": value.session_id,
        }
        for value in values
    ]


def _source_refs_from_json(data: list[dict[str, Any]]) -> tuple[ContextSourceRef, ...]:
    """把摘要JSON里的来源清单还原成引用元组。"""
    return tuple(ContextSourceRef(
        turn_id=ref["turn_id"], revision=ref["revision"], session_id=ref["session_id"],
    ) for ref in data)


def _commitment_to_json(value: Commitment | None) -> dict[str, Any] | None:
    """将 Commitment 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "commitment_id": value.commitment_id,
        "role": value.role,
        "text": value.text,
        "revision": value.revision,
        "status": value.status.value,
        "source_refs": [_source_ref_to_json(item0) for item0 in value.source_refs],
        "decided_on": value.decided_on.isoformat() if value.decided_on is not None else None,
        "planned_for": value.planned_for.isoformat() if value.planned_for is not None else None,
    }


def _commitment_from_json(data: dict[str, Any]) -> Commitment:
    """把JSON对象还原成承诺分区；role不在约定取值内直接报错。"""
    if data["role"] not in ("user", "assistant", "both"):
        raise ValueError("unsupported commitment role")
    return Commitment(
        commitment_id=data["commitment_id"], role=data["role"], text=data["text"],
        revision=data["revision"], status=CommitmentStatus(data["status"]),
        source_refs=_source_refs_from_json(data["source_refs"]),
        decided_on=date.fromisoformat(data["decided_on"]) if data["decided_on"] is not None else None,
        planned_for=date.fromisoformat(data["planned_for"]) if data["planned_for"] is not None else None,
    )


def _object_binding_to_json(value: ObjectBinding | None) -> dict[str, Any] | None:
    """将 ObjectBinding 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "reference": value.reference,
        "object": value.object,
        "source_refs": [_source_ref_to_json(item0) for item0 in value.source_refs],
    }


def _object_binding_from_json(data: dict[str, Any] | None) -> ObjectBinding | None:
    """将 JSON 逐字段还原为 ObjectBinding；缺少字段明确报错。"""
    if data is None:
        return None
    return ObjectBinding(
        reference=data["reference"],
        object=data["object"],
        source_refs=tuple(_source_ref_from_json(item0) for item0 in data["source_refs"]),
    )


def _tone_context_to_json(value: ToneContext | None) -> dict[str, Any] | None:
    """将 ToneContext 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "emotion": value.emotion,
        "temporal_context": value.temporal_context if value.temporal_context is not None else None,
        "current_status": value.current_status if value.current_status is not None else None,
        "source_refs": [_source_ref_to_json(item0) for item0 in value.source_refs],
    }


def _tone_context_from_json(data: dict[str, Any] | None) -> ToneContext | None:
    """将 JSON 逐字段还原为 ToneContext；缺少字段明确报错。"""
    if data is None:
        return None
    return ToneContext(
        emotion=data["emotion"],
        temporal_context=data["temporal_context"] if data["temporal_context"] is not None else None,
        current_status=data["current_status"] if data["current_status"] is not None else None,
        source_refs=tuple(_source_ref_from_json(item0) for item0 in data["source_refs"]),
    )


def _unresolved_question_to_json(value: UnresolvedQuestion | None) -> dict[str, Any] | None:
    """将 UnresolvedQuestion 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "text": value.text,
        "source_refs": [_source_ref_to_json(item0) for item0 in value.source_refs],
    }


def _unresolved_question_from_json(data: dict[str, Any] | None) -> UnresolvedQuestion | None:
    """将 JSON 逐字段还原为 UnresolvedQuestion；缺少字段明确报错。"""
    if data is None:
        return None
    return UnresolvedQuestion(
        text=data["text"],
        source_refs=tuple(_source_ref_from_json(item0) for item0 in data["source_refs"]),
    )


def _latest_topic_to_json(value: LatestTopic | None) -> dict[str, Any] | None:
    """将 LatestTopic 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "text": value.text,
        "source_refs": [_source_ref_to_json(item0) for item0 in value.source_refs],
    }


def _latest_topic_from_json(data: dict[str, Any] | None) -> LatestTopic | None:
    """将 JSON 逐字段还原为 LatestTopic；缺少字段明确报错。"""
    if data is None:
        return None
    return LatestTopic(
        text=data["text"],
        source_refs=tuple(_source_ref_from_json(item0) for item0 in data["source_refs"]),
    )


def _scene_state_to_json(value: SceneState | None) -> dict[str, Any] | None:
    """将 SceneState 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "location": value.location if value.location is not None else None,
        "story_time": value.story_time if value.story_time is not None else None,
        "participants": [item0 for item0 in value.participants],
        "ongoing_actions": [item0 for item0 in value.ongoing_actions],
        "relationship_context": [item0 for item0 in value.relationship_context],
        "character_known_information": [item0 for item0 in value.character_known_information],
        "character_unknown_information": [item0 for item0 in value.character_unknown_information],
        "source_refs": [_source_ref_to_json(item0) for item0 in value.source_refs],
    }


def _scene_state_from_json(data: dict[str, Any] | None) -> SceneState | None:
    """把JSON对象还原成剧情状态分区；传入None返回None。"""
    if data is None:
        return None
    return SceneState(
        location=data["location"], story_time=data["story_time"], participants=tuple(data["participants"]),
        ongoing_actions=tuple(data["ongoing_actions"]), relationship_context=tuple(data["relationship_context"]),
        character_known_information=tuple(data["character_known_information"]),
        character_unknown_information=tuple(data["character_unknown_information"]),
        source_refs=_source_refs_from_json(data["source_refs"]),
    )


def structured_summary_to_json(value: StructuredSummary | None) -> dict[str, Any] | None:
    """将 StructuredSummary 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "summary": value.summary,
        "commitments": [_commitment_to_json(item0) for item0 in value.commitments],
        "active_thread": value.active_thread if value.active_thread is not None else None,
        "object_bindings": [_object_binding_to_json(item0) for item0 in value.object_bindings],
        "tone_context": [_tone_context_to_json(item0) for item0 in value.tone_context],
        "unresolved_questions": [_unresolved_question_to_json(item0) for item0 in value.unresolved_questions],
        "latest_topic": _latest_topic_to_json(value.latest_topic) if value.latest_topic is not None else None,
        "source_refs": [_source_ref_to_json(item0) for item0 in value.source_refs],
        "scene_state": _scene_state_to_json(value.scene_state) if value.scene_state is not None else None,
        "keywords": list(value.keywords),
    }


def structured_summary_from_json(data: dict[str, Any]) -> StructuredSummary:
    """将摘要JSON的所有分区显式还原为业务类型，保留来源、状态和tuple语义。"""
    latest = data["latest_topic"]
    return StructuredSummary(
        summary=data["summary"],
        commitments=tuple(_commitment_from_json(item) for item in data["commitments"]),
        active_thread=data["active_thread"],
        object_bindings=tuple(ObjectBinding(
            reference=item["reference"], object=item["object"],
            source_refs=_source_refs_from_json(item["source_refs"]),
        ) for item in data["object_bindings"]),
        tone_context=tuple(ToneContext(
            emotion=item["emotion"], temporal_context=item["temporal_context"], current_status=item["current_status"],
            source_refs=_source_refs_from_json(item["source_refs"]),
        ) for item in data["tone_context"]),
        unresolved_questions=tuple(UnresolvedQuestion(
            text=item["text"], source_refs=_source_refs_from_json(item["source_refs"]),
        ) for item in data["unresolved_questions"]),
        latest_topic=LatestTopic(latest["text"], _source_refs_from_json(latest["source_refs"]))
        if latest is not None else None,
        source_refs=_source_refs_from_json(data["source_refs"]),
        scene_state=_scene_state_from_json(data["scene_state"]),
        keywords=tuple(data["keywords"]),
    )


def _summary_versions_to_json(value: SummaryVersions | None) -> dict[str, Any] | None:
    """将 SummaryVersions 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "version": value.version,
        "prompt_version": value.prompt_version,
        "model_name": value.model_name,
        "tokenizer_version": value.tokenizer_version,
    }


def _summary_versions_from_json(data: dict[str, Any] | None) -> SummaryVersions | None:
    """将 JSON 逐字段还原为 SummaryVersions；缺少字段明确报错。"""
    if data is None:
        return None
    return SummaryVersions(
        version=data["version"],
        prompt_version=data["prompt_version"],
        model_name=data["model_name"],
        tokenizer_version=data["tokenizer_version"],
    )


def _context_prompt_budget_to_json(value: ContextPromptBudget | None) -> dict[str, Any] | None:
    """将 ContextPromptBudget 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "effective_context_limit": value.effective_context_limit,
        "reserved_output_tokens": value.reserved_output_tokens,
        "character_card_budget": value.character_card_budget,
        "history_budget": value.history_budget,
        "recalled_memory_budget": value.recalled_memory_budget,
        "current_turn_budget": value.current_turn_budget,
        "scene_reference_budget": value.scene_reference_budget,
        "safety_margin": value.safety_margin,
    }


def _context_prompt_budget_from_json(data: dict[str, Any] | None) -> ContextPromptBudget | None:
    """将 JSON 逐字段还原为 ContextPromptBudget；缺少字段明确报错。"""
    if data is None:
        return None
    return ContextPromptBudget(
        effective_context_limit=data["effective_context_limit"],
        reserved_output_tokens=data["reserved_output_tokens"],
        character_card_budget=data["character_card_budget"],
        history_budget=data["history_budget"],
        recalled_memory_budget=data["recalled_memory_budget"],
        current_turn_budget=data["current_turn_budget"],
        scene_reference_budget=data["scene_reference_budget"],
        safety_margin=data["safety_margin"],
    )


def _compaction_policy_to_json(value: ContextCompactionPolicy | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "schema_version": 3,
        "summary_token_limit": value.summary_token_limit,
        "summary_target_tokens": value.summary_target_tokens,
        "max_compression_retries": value.max_compression_retries,
    }


def _compaction_policy_from_json(data: dict[str, Any] | None) -> ContextCompactionPolicy | None:
    if data is None:
        return None
    if type(data["schema_version"]) is not int or data["schema_version"] != 3:
        raise ValueError("unsupported compaction policy schema")
    return ContextCompactionPolicy(
        summary_token_limit=data["summary_token_limit"],
        summary_target_tokens=data["summary_target_tokens"],
        max_compression_retries=data["max_compression_retries"],
    )


def compaction_plan_to_json(value: CompactionPlan | None) -> dict[str, Any] | None:
    """将 CompactionPlan 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "schema_version": 3,
        "session_id": value.session_id,
        "mode": value.mode.value,
        "source_turns": SQLiteValueCodec.range_to_json(value.source_turns) if value.source_turns is not None else None,
        "retained_tail": SQLiteValueCodec.range_to_json(value.retained_tail) if value.retained_tail is not None else None,
        "prompt_budget": _context_prompt_budget_to_json(value.prompt_budget),
        "compaction_policy": _compaction_policy_to_json(value.compaction_policy),
        "inherited_context": SQLiteValueCodec.inherited_context_to_json(value.inherited_context) if value.inherited_context is not None else None,
        "context_only_turns": SQLiteValueCodec.range_to_json(value.context_only_turns) if value.context_only_turns is not None else None,
        "planned_summary_tokens": value.planned_summary_tokens,
    }


def compaction_plan_from_json(data: dict[str, Any] | None) -> CompactionPlan | None:
    """将 JSON 逐字段还原为 CompactionPlan；缺少字段明确报错。"""
    if data is None:
        return None
    if type(data["schema_version"]) is not int or data["schema_version"] != 3:
        raise ValueError("unsupported context plan schema")
    return CompactionPlan(
        session_id=data["session_id"],
        mode=CompactionMode(data["mode"]),
        source_turns=SQLiteValueCodec.range_from_json(data["source_turns"]) if data["source_turns"] is not None else None,
        retained_tail=SQLiteValueCodec.range_from_json(data["retained_tail"]) if data["retained_tail"] is not None else None,
        prompt_budget=_context_prompt_budget_from_json(data["prompt_budget"]),
        compaction_policy=_compaction_policy_from_json(data["compaction_policy"]),
        inherited_context=SQLiteValueCodec.inherited_context_from_json(data["inherited_context"]) if data["inherited_context"] is not None else None,
        context_only_turns=SQLiteValueCodec.range_from_json(data["context_only_turns"]) if data["context_only_turns"] is not None else None,
        planned_summary_tokens=data["planned_summary_tokens"],
    )


def source_manifest_to_json(value: SummarySourceManifest | None) -> dict[str, Any] | None:
    """将 SummarySourceManifest 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "schema_version": 3,
        "session_id": value.session_id,
        "source_turns": SQLiteValueCodec.range_to_json(value.source_turns) if value.source_turns is not None else None,
        "source_refs": [_source_ref_to_json(item0) for item0 in value.source_refs],
        "source_hash": value.source_hash,
        "inherited_context": SQLiteValueCodec.inherited_context_to_json(value.inherited_context) if value.inherited_context is not None else None,
        "inherited_context_included": value.inherited_context_included,
    }


def source_manifest_from_json(data: dict[str, Any] | None) -> SummarySourceManifest | None:
    """将 JSON 逐字段还原为 SummarySourceManifest；缺少字段明确报错。"""
    if data is None:
        return None
    if type(data["schema_version"]) is not int or data["schema_version"] != 3:
        raise ValueError("unsupported context source schema")
    return SummarySourceManifest(
        session_id=data["session_id"],
        source_turns=SQLiteValueCodec.range_from_json(data["source_turns"]) if data["source_turns"] is not None else None,
        source_refs=tuple(_source_ref_from_json(item0) for item0 in data["source_refs"]),
        source_hash=data["source_hash"],
        inherited_context=SQLiteValueCodec.inherited_context_from_json(data["inherited_context"]) if data["inherited_context"] is not None else None,
        inherited_context_included=data["inherited_context_included"],
    )


def summary_candidate_to_json(value: SummaryCandidate | None) -> dict[str, Any] | None:
    """将 SummaryCandidate 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "candidate_version": value.candidate_version,
        "source_manifest": source_manifest_to_json(value.source_manifest),
        "covered_through_turn_seq": value.covered_through_turn_seq,
        "summary": structured_summary_to_json(value.summary),
        "summary_text": value.summary_text,
        "token_count": value.token_count,
        "content_hash": value.content_hash,
        "versions": _summary_versions_to_json(value.versions),
        "generated_at": SQLiteValueCodec.datetime_to_json(value.generated_at),
        "attempt_version": value.attempt_version,
    }


def summary_candidate_from_json(data: dict[str, Any] | None) -> SummaryCandidate | None:
    """将 JSON 逐字段还原为 SummaryCandidate；缺少字段明确报错。"""
    if data is None:
        return None
    return SummaryCandidate(
        candidate_version=data["candidate_version"],
        source_manifest=source_manifest_from_json(data["source_manifest"]),
        covered_through_turn_seq=data["covered_through_turn_seq"],
        summary=structured_summary_from_json(data["summary"]),
        summary_text=data["summary_text"],
        token_count=data["token_count"],
        content_hash=data["content_hash"],
        versions=_summary_versions_from_json(data["versions"]),
        generated_at=SQLiteValueCodec.datetime_from_json(data["generated_at"]),
        attempt_version=data["attempt_version"],
    )


def validation_to_json(
    value: SummaryValidation | None,
) -> dict[str, Any] | None:
    """把程序校验结果转成 JSON 对象。"""
    if value is None:
        return None

    return {
        "candidate_version": value.candidate_version,
        "passed": value.passed,
        "issues": list(value.issues),
        "validation_version": value.validation_version,
        "candidate_hash": value.candidate_hash,
    }


def validation_from_json(
    data: dict[str, Any] | None,
) -> SummaryValidation | None:
    """把 JSON 对象还原成程序校验结果。"""
    if data is None:
        return None

    return SummaryValidation(
        candidate_version=data["candidate_version"],
        passed=data["passed"],
        issues=tuple(data["issues"]),
        validation_version=data["validation_version"],
        candidate_hash=data["candidate_hash"],
    )


def handoff_packet_to_json(value: HandoffPacket | None) -> dict[str, Any] | None:
    """将 HandoffPacket 逐字段转换为 JSON；None 保持为空。"""
    if value is None:
        return None
    return {
        "handoff_id": value.handoff_id,
        "predecessor_session_id": value.predecessor_session_id,
        "fallback_snapshot_ref": SQLiteValueCodec.snapshot_to_json(value.fallback_snapshot_ref) if value.fallback_snapshot_ref is not None else None,
        "raw_range": _inherited_range_to_json(value.raw_range) if value.raw_range is not None else None,
        "source_hash": value.source_hash,
        "mode": value.mode.value,
        "structured_summary": structured_summary_to_json(value.structured_summary),
        "prepared_through_turn_seq": value.prepared_through_turn_seq,
        "summary_covered_through_turn_seq": value.summary_covered_through_turn_seq,
        "switch_through_turn_seq": value.switch_through_turn_seq,
    }


def handoff_packet_from_json(data: dict[str, Any] | None) -> HandoffPacket | None:
    """将 JSON 逐字段还原为 HandoffPacket；缺少字段明确报错。"""
    if data is None:
        return None
    return HandoffPacket(
        handoff_id=data["handoff_id"],
        predecessor_session_id=data["predecessor_session_id"],
        fallback_snapshot_ref=SQLiteValueCodec.snapshot_from_json(data["fallback_snapshot_ref"]) if data["fallback_snapshot_ref"] is not None else None,
        raw_range=_inherited_range_from_json(data["raw_range"]) if data["raw_range"] is not None else None,
        source_hash=data["source_hash"],
        mode=HandoffMode(data["mode"]),
        structured_summary=structured_summary_from_json(data["structured_summary"]),
        prepared_through_turn_seq=data["prepared_through_turn_seq"],
        summary_covered_through_turn_seq=data["summary_covered_through_turn_seq"],
        switch_through_turn_seq=data["switch_through_turn_seq"],
    )


def context_job_registration_to_row(
    value: ContextJob,
) -> ContextJobRow:
    """将新登记的上下文任务转换为 ORM 行，不用于已有任务更新。

    登记时尚未固定 plan，也没有生成、校验或发布产物。
    后续阶段修改已有行，不能用本方法覆盖阶段数据。
    """
    return ContextJobRow(
        job_id=value.job_id,
        job_type=value.job_type.value,
        session_id=value.session_id,
        target_key=value.target_key,
        dedupe_key=value.dedupe_key,
        priority=value.priority,
        status=value.status.value,
        retry_count=value.retry_count,
        available_at=value.available_at,
        created_at=value.created_at,
        updated_at=value.updated_at,
        payload_json={
            "schema_version": 4,
            "frozen_through_turn_seq": value.frozen_through_turn_seq,
            "plan": None,
            "source_manifest": None,
            "candidate": None,
            "validation": None,
            "handoff_packet": None,
            "published_snapshot": None,
            "last_error": None,
            "finished_at": None,
            "attempt_version": 0,
            "attempt_count": 0,
        },
    )


def turn_token_info_from_row(row: TurnRow) -> TurnTokenInfo:
    """将已校验的完整轮统计行转换为纯业务投影，不读取数据库。"""
    return TurnTokenInfo(
        turn_id=row.turn_id,
        session_id=row.session_id,
        turn_seq=row.turn_seq,
        token_count=row.turn_token_count,
        cumulative_token_count=row.cumulative_token_count,
        tokenizer_version=row.tokenizer_version,
    )
