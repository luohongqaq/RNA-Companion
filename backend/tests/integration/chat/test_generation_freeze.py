"""T2-B 冻结测试：使用真实 T1/T2-A 准备状态，固定材料代替模型输出。"""

import hashlib
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from module.chat.conversation.core.domain.contracts import SaveMessageInput
from module.shared.common.errors.base import InvalidStateError
from module.chat.reply.core.domain.fingerprint import ReplyFingerprint
from module.chat.reply.core.domain.contracts import GenerationFreezeTicket
from module.chat.reply.core.domain.materials import (
    ChatContextManifest, FrozenPrompt, MemoryManifest, PromptMessage,
)
from module.memory_recall.agent.core.domain.audit import MemoryAudit
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from storage.sqlite.models import (
    CharacterRow, GenerationAttemptRow, GenerationJobRow, MessageRow, TurnRow,
)


@pytest_asyncio.fixture
async def prepared_freeze(tmp_path):
    """两条用户输入，便于检查冻结时不能漏消息、改正文或颠倒顺序。"""
    database = SQLiteDatabase(tmp_path / "t2b.sqlite3")
    try:
        await database.initialize()
        async with database.transaction() as session:
            session.add(CharacterRow(
                character_id="character_test", name="测试角色", character_setting="你是桌宠。",
            ))
        chat = SQLiteChatRepository(database)
        repository = SQLiteGenerationRepository(database)
        command = SaveMessageInput("request_001", "conversation_001", "character_test", "今天有点累")
        await chat.accept_message(command)
        saved = await chat.accept_message(replace(command, request_id="request_002", content="想休息一下"))
        started = await repository.claim_job(saved.job_id)
        assert started is not None
        boundary = started.input_boundary
        prompt = FrozenPrompt(
            messages=(PromptMessage("system", "你是桌宠。"),
                      PromptMessage("user", command.content), PromptMessage("user", "想休息一下")),
            token_count=None,
            context_manifest=ChatContextManifest(
                boundary.working_snapshot, boundary.complete_turns,
                boundary.current_turn, boundary.inherited_context,
            ),
            memory_manifest=MemoryManifest(),
            character_card_version="test-card", tokenizer_version="unconfigured",
            character_id="character_test",
        )
        ticket = GenerationFreezeTicket(
            saved.job_id, started.attempt_id, saved.turn_id, boundary.session_id,
            started.input_version, started.active_token, prompt,
        )
        yield database, chat, repository, ticket
    finally:
        await database.close()


async def assert_not_frozen(database, ticket):
    """使用新 Session 观察事务外状态，不能只看异常或方法返回值。"""
    async with database.db_session() as session:
        attempt = await session.get(GenerationAttemptRow, ticket.attempt_id)
        job = await session.get(GenerationJobRow, ticket.job_id)
        assert attempt.attempt_state == "retrieving"
        assert job.state == "need_prepare"
        assert job.current_attempt_id == attempt.attempt_id
        assert attempt.frozen_prompt_json is None
        assert attempt.context_manifest_json is None
        assert attempt.memory_manifest_json is None
        assert attempt.memory_audit_json is None
        assert attempt.prompt_hash is None


def with_memory(ticket):
    """固定一份记忆总结及审计，不执行记忆检索或总结模型。"""
    summary = "用户之前提到最近工作很忙。"
    digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()
    source = MemoryRevisionRef(MemoryKind.TOPIC, "topic_test", 1, "test-content-hash")
    prompt = replace(
        ticket.frozen_prompt,
        messages=(PromptMessage("system", summary), *ticket.frozen_prompt.messages),
        memory_summary=summary, memory_summary_hash=digest,
        memory_manifest=MemoryManifest(topic=(source,)),
    )
    audit = MemoryAudit(
        attempt_id=ticket.attempt_id, status="ready", source_refs=(source,),
        source_cards=({"memory_id": "topic_test", "content": summary},),
        summary_hash=digest, summary_status="ready",
        reason_code="test", unresolved=(), skill_hash="test-skill",
        summary_prompt_hash="test-summary", character_view_hash="test-character",
        total_seconds=0.1, trajectory=(),
    )
    return replace(ticket, frozen_prompt=prompt, memory_audit=audit)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_memory", [False, True], ids=["empty-memory_record", "with-summary"])
async def test_freeze_commits_materials_and_advances_stage(prepared_freeze, has_memory):
    """正确冻结提示词材料并提交 + 推进任务阶段”"""
    database, _, repository, ticket = prepared_freeze
    if has_memory:
        ticket = with_memory(ticket)

    assert await repository.freeze_prompt(ticket) is True

    # 通过真实映射读回，验证提交后的材料，而非仅验证写入参数。
    attempt = await repository.get_attempt(ticket.attempt_id)
    assert attempt.frozen_prompt == ticket.frozen_prompt
    assert attempt.memory_audit == ticket.memory_audit
    async with database.db_session() as session:
        row = await session.get(GenerationAttemptRow, ticket.attempt_id)
        job = await session.get(GenerationJobRow, ticket.job_id)
        turn = await session.get(TurnRow, ticket.turn_id)
        assert row.attempt_state == "generating"
        assert row.prompt_hash == ReplyFingerprint.prompt_hash(ticket.frozen_prompt.messages)
        assert row.context_manifest_json == row.frozen_prompt_json["context_manifest"]
        assert row.memory_manifest_json == row.frozen_prompt_json["memory_manifest"]
        assert job.state == "need_generate"
        assert turn.turn_state == "current"
        assert turn.accepted_message_id is None
        messages = (await session.scalars(select(MessageRow))).all()
        assert len(messages) == 2
        assert all(message.role == "user" for message in messages)


@pytest.mark.asyncio
async def test_new_input_rejects_old_freeze_without_harming_new_job(prepared_freeze):
    """新输入会撤回旧冻结，但不影响新作业"""
    database, chat, repository, ticket = prepared_freeze
    new = await chat.accept_message(SaveMessageInput(
        "request_003", "conversation_001", "character_test", "还有点饿",
    ))

    assert await repository.freeze_prompt(ticket) is False

    async with database.db_session() as session:
        old_attempt = await session.get(GenerationAttemptRow, ticket.attempt_id)
        old_job = await session.get(GenerationJobRow, ticket.job_id)
        new_job = await session.get(GenerationJobRow, new.job_id)
        turn = await session.get(TurnRow, new.turn_id)
        assert old_attempt.attempt_state == "superseded"
        assert old_attempt.frozen_prompt_json is None
        assert old_job.state == "obsolete"
        assert new_job.state == "need_prepare"
        assert new_job.expected_input_version == turn.input_version == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["job_id", "attempt_id", "turn_id", "session_id", "input_version", "active_token"])
async def test_foreign_or_stale_ticket_is_rejected(prepared_freeze, field):
    """任意字段对不上都不能冻结"""
    database, _, repository, ticket = prepared_freeze
    original = getattr(ticket, field)
    changed = original + 1 if isinstance(original, int) else "foreign_id"

    assert await repository.freeze_prompt(replace(ticket, **{field: changed})) is False

    await assert_not_frozen(database, ticket)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["empty", "missing", "rewritten", "reordered", "boundary", "character"])
async def test_invalid_prompt_does_not_freeze(prepared_freeze, change):
    """提示词内容层面的完整性校验，主要是用户消息、输入边界不能变，freeze_prompt会检查"""
    database, _, repository, ticket = prepared_freeze
    prompt = ticket.frozen_prompt
    if change == "empty":
        prompt = replace(prompt, messages=())
    elif change == "missing":
        prompt = replace(prompt, messages=(prompt.messages[0], prompt.messages[-1]))
    elif change == "rewritten":
        prompt = replace(prompt, messages=(*prompt.messages[:-1], PromptMessage("user", "改写输入")))
    elif change == "reordered":
        prompt = replace(prompt, messages=(prompt.messages[0], prompt.messages[2], prompt.messages[1]))
    elif change == "boundary":
        current = replace(prompt.context_manifest.current_turn, input_version=99)
        prompt = replace(prompt, context_manifest=replace(prompt.context_manifest, current_turn=current))
    else:
        prompt = replace(prompt, character_id="other_character")

    with pytest.raises(InvalidStateError):
        await repository.freeze_prompt(replace(ticket, frozen_prompt=prompt))

    await assert_not_frozen(database, ticket)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["summary-hash", "no-audit", "audit-attempt", "audit-hash", "sources", "incomplete", "not-in-messages"])
async def test_inconsistent_memory_does_not_freeze(prepared_freeze, change):
    """记忆总结层面的完整性校验，主要是记忆内容不能变，freeze_prompt会检查"""
    database, _, repository, original = prepared_freeze
    ticket = with_memory(original)
    if change == "summary-hash":
        ticket = replace(ticket, frozen_prompt=replace(ticket.frozen_prompt, memory_summary_hash="broken"))
    elif change == "no-audit":
        ticket = replace(ticket, memory_audit=None)
    elif change == "audit-attempt":
        ticket = replace(ticket, memory_audit=replace(ticket.memory_audit, attempt_id="other_attempt"))
    elif change == "audit-hash":
        ticket = replace(ticket, memory_audit=replace(ticket.memory_audit, summary_hash="broken"))
    elif change == "sources":
        ticket = replace(ticket, memory_audit=replace(ticket.memory_audit, source_refs=()))
    elif change == "incomplete":
        ticket = replace(ticket, memory_audit=replace(ticket.memory_audit, status="incomplete"))
    else:
        ticket = replace(ticket, frozen_prompt=replace(ticket.frozen_prompt, messages=original.frozen_prompt.messages))

    with pytest.raises(InvalidStateError):
        await repository.freeze_prompt(ticket)

    await assert_not_frozen(database, original)


@pytest.mark.asyncio
async def test_repeated_freeze_does_not_overwrite_committed_prompt(prepared_freeze):
    """同一张票据冻结成功后，第二次带着不同内容的冻结请求必须返回 False"""
    _, _, repository, ticket = prepared_freeze
    assert await repository.freeze_prompt(ticket) is True
    changed = replace(ticket, frozen_prompt=replace(
        ticket.frozen_prompt,
        messages=(PromptMessage("system", "另一套系统提示词"), *ticket.frozen_prompt.messages[1:]),
    ))

    assert await repository.freeze_prompt(changed) is False

    attempt = await repository.get_attempt(ticket.attempt_id)
    assert attempt.frozen_prompt == ticket.frozen_prompt


@pytest.mark.asyncio
async def test_failed_stage_update_rolls_back_frozen_materials(prepared_freeze):
    """冻结失败时会回滚冻结材料，失败重试时同一票据可再次冻结"""
    database, _, repository, ticket = prepared_freeze
    ticket = with_memory(ticket)
    # 让阶段推进在真实 SQL UPDATE 时失败；冻结列和阶段必须在同一事务回滚。
    async with database.transaction() as session:
        await session.execute(text("""
            CREATE TRIGGER fail_freeze_stage
            BEFORE UPDATE OF state ON generation_jobs
            WHEN NEW.state = 'need_generate'
            BEGIN
                SELECT RAISE(ABORT, 'simulated freeze failure');
            END;
        """))

    with pytest.raises(IntegrityError, match="simulated freeze failure"):
        await repository.freeze_prompt(ticket)

    await assert_not_frozen(database, ticket)
    # 撤掉故障后，同一票据可再次冻结，证明失败没有破坏采用资格。
    async with database.transaction() as session:
        await session.execute(text("DROP TRIGGER fail_freeze_stage"))
    assert await repository.freeze_prompt(ticket) is True
