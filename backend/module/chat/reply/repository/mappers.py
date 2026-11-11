"""回复任务、生成尝试与冻结提示词的 SQLite 映射。"""

import copy
from dataclasses import asdict
from module.shared.character.types.manifest import VisualOptions
from typing import Any
from module.character.core.domain.models import CharacterReply
from module.shared.context.types.turns import CurrentTurnRef
from module.chat.reply.core.domain.fingerprint import ReplyFingerprint
from module.chat.reply.core.domain.materials import (
    ChatContextManifest,
    FrozenPrompt,
    GenerationInputBoundary,
    MemoryManifest,
    ModelUsage,
    PromptMessage,
    ReplyCandidate,
)
from module.chat.reply.core.domain.models import (
    AttemptState,
    GenerationAttempt,
    GenerationJob,
    GenerationJobStatus,
)
from module.memory_recall.agent.core.domain.audit import MemoryAudit
from storage.sqlite.models import GenerationAttemptRow, GenerationJobRow
from module.chat.conversation.repository.mappers import (
    failure_from_json,
    failure_to_json,
    visual_from_json,
    visual_to_json,
)
from storage.sqlite.value_codecs import SQLiteValueCodec


def generation_job_to_row(value: GenerationJob) -> GenerationJobRow:
    """把生成任务业务对象转成新行，仅用于插入；最近一次错误序列化为failure_json。"""
    return GenerationJobRow(job_id=value.job_id, turn_id=value.turn_id,
        expected_input_version=value.expected_input_version, state=value.status.value, priority=value.priority,
        available_at=value.available_at, dedupe_key=value.dedupe_key, created_at=value.created_at,
        updated_at=value.updated_at, current_attempt_id=value.current_attempt_id, attempt_count=value.attempt_count,
        retry_count=value.retry_count, manual_retry_count=value.manual_retry_count,
        failure_json=failure_to_json(value.last_error),
        finished_at=value.finished_at)


def generation_job_to_domain(row: GenerationJobRow) -> GenerationJob:
    """把生成任务行还原成业务对象，failure_json反序列化为last_error。"""
    failure = failure_from_json(row.failure_json)
    return GenerationJob(row.job_id, row.turn_id, row.expected_input_version,
        GenerationJobStatus(row.state), row.priority, row.available_at, row.dedupe_key,
        row.created_at, row.updated_at, row.current_attempt_id, row.attempt_count,
        row.retry_count, failure, row.finished_at, manual_retry_count=row.manual_retry_count)


def generation_attempt_to_row(value: GenerationAttempt, *, prompt_hash: str | None = None) -> GenerationAttemptRow:
    """把生成尝试业务对象转成新行；冻结提示词拆存为正文列加context/memory清单两列。"""
    if value.candidate is not None:
        if prompt_hash is not None and prompt_hash != value.candidate.prompt_hash:
            raise ValueError("candidate and supplied prompt hashes differ")
        prompt_hash = value.candidate.prompt_hash
    if value.frozen_prompt is not None and prompt_hash is None:
        raise ValueError("the frozen prompt hash must be supplied by the generation use case")
    frozen_prompt = _frozen_prompt_to_json(value.frozen_prompt)
    return GenerationAttemptRow(attempt_id=value.attempt_id, job_id=value.job_id, turn_id=value.turn_id,
        attempt_version=value.attempt_seq, input_version=value.input_version, active_token=value.active_token,
        attempt_state=value.state.value, input_boundary_json=boundary_to_json(value.input_boundary),
        frozen_prompt_json=frozen_prompt, candidate_json=_candidate_to_json(value.candidate),
        failure_json=failure_to_json(value.failure), created_at=value.created_at, updated_at=value.updated_at,
        completed_at=value.finished_at, prompt_hash=prompt_hash,
        context_manifest_json=frozen_prompt["context_manifest"] if frozen_prompt is not None else None,
        memory_manifest_json=frozen_prompt["memory_manifest"] if frozen_prompt is not None else None,
        memory_audit_json=_memory_audit_to_json(value.memory_audit))


def generation_attempt_to_domain(row: GenerationAttemptRow) -> GenerationAttempt:
    """把生成尝试行还原成业务对象；input_boundary_json缺失视为存储不完整，直接报错。"""
    if row.input_boundary_json is None:
        raise ValueError("attempt is missing its fixed input boundary")
    return GenerationAttempt(row.attempt_id, row.job_id, row.turn_id, row.attempt_version,
        row.input_version, row.active_token, AttemptState(row.attempt_state),
        boundary_from_json(row.input_boundary_json), row.created_at,
        row.updated_at,
        _frozen_prompt_from_json(row.frozen_prompt_json),
        _candidate_from_json(row.candidate_json),
        failure_from_json(row.failure_json),
        row.completed_at, memory_audit=_memory_audit_from_json(row.memory_audit_json))


def _current_turn_to_json(value: CurrentTurnRef) -> dict[str, Any]:
    """把当前轮引用转成JSON对象，message_ids转为列表。"""
    return {"turn_id": value.turn_id, "input_version": value.input_version, "message_ids": list(value.message_ids)}


def _current_turn_from_json(data: dict[str, Any]) -> CurrentTurnRef:
    """把JSON对象还原成当前轮引用，message_ids还原为元组。"""
    return CurrentTurnRef(turn_id=data["turn_id"], input_version=data["input_version"],
                          message_ids=tuple(data["message_ids"]))


def boundary_to_json(value: GenerationInputBoundary) -> dict[str, Any]:
    """把生成输入边界转成JSON对象。"""
    return {
        "conversation_id": value.conversation_id, "character_id": value.character_id, "session_id": value.session_id,
        "working_snapshot": SQLiteValueCodec.snapshot_to_json(value.working_snapshot),
        "complete_turns": SQLiteValueCodec.range_to_json(value.complete_turns),
        "current_turn": _current_turn_to_json(value.current_turn),
        "inherited_context": SQLiteValueCodec.inherited_context_to_json(value.inherited_context),
    }


def boundary_from_json(data: dict[str, Any]) -> GenerationInputBoundary:
    """把JSON对象还原成生成输入边界。"""
    return GenerationInputBoundary(
        conversation_id=data["conversation_id"], character_id=data["character_id"], session_id=data["session_id"],
        working_snapshot=SQLiteValueCodec.snapshot_from_json(data["working_snapshot"]),
        complete_turns=SQLiteValueCodec.range_from_json(data["complete_turns"]),
        current_turn=_current_turn_from_json(data["current_turn"]),
        inherited_context=SQLiteValueCodec.inherited_context_from_json(data["inherited_context"]),
    )


def _context_to_json(value: ChatContextManifest) -> dict[str, Any]:
    """把聊天上下文清单转成JSON对象。"""
    return {
        "working_snapshot": SQLiteValueCodec.snapshot_to_json(value.working_snapshot),
        "complete_turns": SQLiteValueCodec.range_to_json(value.complete_turns),
        "current_turn": _current_turn_to_json(value.current_turn),
        "inherited_context": SQLiteValueCodec.inherited_context_to_json(value.inherited_context),
    }


def _context_from_json(data: dict[str, Any]) -> ChatContextManifest:
    """把JSON对象还原成聊天上下文清单。"""
    return ChatContextManifest(
        working_snapshot=SQLiteValueCodec.snapshot_from_json(data["working_snapshot"]),
        complete_turns=SQLiteValueCodec.range_from_json(data["complete_turns"]),
        current_turn=_current_turn_from_json(data["current_turn"]),
        inherited_context=SQLiteValueCodec.inherited_context_from_json(data["inherited_context"]),
    )


def _frozen_prompt_to_json(value: FrozenPrompt | None) -> dict[str, Any] | None:
    """把冻结提示词转成JSON对象，含消息、token数、上下文与记忆清单。"""
    if value is None:
        return None
    return {
        "messages": [{"role": message.role, "content": message.content} for message in value.messages],
        "token_count": value.token_count,
        "context_manifest": _context_to_json(value.context_manifest),
        "memory_manifest": {
            "daily": [SQLiteValueCodec.memory_ref_to_json(ref) for ref in value.memory_manifest.daily],
            "topic": [SQLiteValueCodec.memory_ref_to_json(ref) for ref in value.memory_manifest.topic],
            "other": [SQLiteValueCodec.memory_ref_to_json(ref) for ref in value.memory_manifest.other],
        },
        "character_card_version": value.character_card_version, "tokenizer_version": value.tokenizer_version,
        "template_version": value.template_version,
        "tone_prompt": value.tone_prompt, "tone_prompt_version": value.tone_prompt_version,
        "memory_summary": value.memory_summary, "memory_summary_hash": value.memory_summary_hash,
        "budget_checked": value.budget_checked,
          "character_id": value.character_id, "current_visual": visual_to_json(value.current_visual),
          "pose_consecutive_turns": value.pose_consecutive_turns,
          "polish_context_hash": ReplyFingerprint.polish_context_hash(value),
          "generation_context_hash": ReplyFingerprint.generation_context_hash(value),
          "scene_examples": copy.deepcopy(list(value.scene_examples)),
          "anchor_position": value.anchor_position,
          "visual_options": asdict(value.visual_options) if value.visual_options is not None else None,
    }


def _frozen_prompt_from_json(data: dict[str, Any] | None) -> FrozenPrompt | None:
    """把JSON对象还原成冻结提示词；角色不在约定取值内直接报错。"""
    if data is None:
        return None
    messages = []
    for message in data["messages"]:
        if message["role"] not in ("system", "developer", "user", "assistant"):
            raise ValueError("unsupported prompt message role")
        messages.append(PromptMessage(role=message["role"], content=message["content"]))
    memory = data["memory_manifest"]
    options = data.get("visual_options")
    return FrozenPrompt(
        messages=tuple(messages), token_count=data["token_count"],
        context_manifest=_context_from_json(data["context_manifest"]),
        memory_manifest=MemoryManifest(
            daily=tuple(SQLiteValueCodec.memory_ref_from_json(ref) for ref in memory["daily"]),
            topic=tuple(SQLiteValueCodec.memory_ref_from_json(ref) for ref in memory["topic"]),
            other=tuple(SQLiteValueCodec.memory_ref_from_json(ref) for ref in memory["other"]),
        ),
        character_card_version=data["character_card_version"], tokenizer_version=data["tokenizer_version"],
        template_version=data["template_version"], tone_prompt=data["tone_prompt"],
        tone_prompt_version=data["tone_prompt_version"],
        memory_summary=data["memory_summary"], memory_summary_hash=data["memory_summary_hash"],
        budget_checked=data["budget_checked"],
        character_id=data.get("character_id", ""),
        current_visual=visual_from_json(data.get("current_visual")),
        pose_consecutive_turns=data.get("pose_consecutive_turns", 0),
        visual_options=VisualOptions(tuple(options["poses"]), tuple(options["expressions"])) if options is not None else None,
        scene_examples=tuple(copy.deepcopy(data.get("scene_examples", []))),
        anchor_position=data.get("anchor_position", "system_end"),
    )


def _memory_audit_to_json(value: MemoryAudit | None) -> dict[str, Any] | None:
    """原始卡片与轨迹独立保存，禁止混入frozen_prompt_json。"""
    if value is None:
        return None
    return {
        "attempt_id": value.attempt_id, "status": value.status,
        "source_refs": [SQLiteValueCodec.memory_ref_to_json(ref) for ref in value.source_refs],
        "source_cards": copy.deepcopy(list(value.source_cards)),
        "summary_hash": value.summary_hash, "summary_status": value.summary_status,
        "reason_code": value.reason_code, "unresolved": list(value.unresolved),
        "skill_hash": value.skill_hash, "summary_prompt_hash": value.summary_prompt_hash,
        "character_view_hash": value.character_view_hash, "total_seconds": value.total_seconds,
        "trajectory": copy.deepcopy(list(value.trajectory)),
    }


def _memory_audit_from_json(data: dict[str, Any] | None) -> MemoryAudit | None:
    if data is None:
        return None
    if data["status"] not in ("ready", "empty", "incomplete"):
        raise ValueError("unsupported memory_record audit status")
    return MemoryAudit(
        attempt_id=data["attempt_id"], status=data["status"],
        source_refs=tuple(SQLiteValueCodec.memory_ref_from_json(ref) for ref in data["source_refs"]),
        source_cards=tuple(copy.deepcopy(data["source_cards"])),
        summary_hash=data["summary_hash"], summary_status=data["summary_status"],
        reason_code=data["reason_code"], unresolved=tuple(data["unresolved"]),
        skill_hash=data["skill_hash"], summary_prompt_hash=data["summary_prompt_hash"],
        character_view_hash=data["character_view_hash"], total_seconds=data["total_seconds"],
        trajectory=tuple(copy.deepcopy(data["trajectory"])),
    )


def _candidate_to_json(value: ReplyCandidate | None) -> dict[str, Any] | None:
    """把回复候选转成JSON对象，时间转ISO文本。"""
    if value is None:
        return None
    return {
        "reply": {"response_zh": value.reply.response_zh, "visual": visual_to_json(value.reply.visual)},
        "candidate_hash": value.candidate_hash, "prompt_hash": value.prompt_hash,
        "usage": ({"input_tokens": value.usage.input_tokens, "output_tokens": value.usage.output_tokens,
                   "total_tokens": value.usage.total_tokens} if value.usage is not None else None),
          "created_at": SQLiteValueCodec.datetime_to_json(value.created_at),
        "generation_trace": value.generation_trace,
    }


def _candidate_from_json(data: dict[str, Any] | None) -> ReplyCandidate | None:
    """把JSON对象还原成回复候选；缺视觉状态直接报错。"""
    if data is None:
        return None
    visual = visual_from_json(data["reply"]["visual"])
    if visual is None:
        raise ValueError("candidate reply must include a visual state")
    return ReplyCandidate(
        reply=CharacterReply(response_zh=data["reply"]["response_zh"], visual=visual),
        candidate_hash=data["candidate_hash"], prompt_hash=data["prompt_hash"],
        usage=(ModelUsage(input_tokens=data["usage"]["input_tokens"], output_tokens=data["usage"]["output_tokens"],
                          total_tokens=data["usage"]["total_tokens"]) if data["usage"] is not None else None),
        created_at=SQLiteValueCodec.datetime_from_json(data["created_at"]),
        generation_trace=data.get("generation_trace"),
    )
