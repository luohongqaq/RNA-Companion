"""单阶段候选经真实SQLite保存、恢复、采用；校验场景与锚点证据。"""
from dataclasses import replace
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select

from module.chat.conversation.core.domain.contracts import SaveMessageInput
from module.shared.common.errors.base import InvalidStateError
from module.chat.reply.core.processing.completion import GenerationCompletionService
from module.chat.reply.core.processing.service import GenerationReplyService
from module.chat.reply.core.domain.fingerprint import ReplyFingerprint
from module.chat.reply.core.domain.contracts import GenerationFreezeTicket, ReplyAcceptanceTicket
from module.chat.reply.core.domain.materials import FrozenPrompt, ChatContextManifest, MemoryManifest, PromptMessage
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from storage.sqlite.models import CharacterRow, GenerationAttemptRow, MessageRow
from module.chat.reply.repository.llm import ReplyModelRepository


class Scenes:
    async def search(self, query):
        return ()


class Client:
    def __init__(self):
        self.chat = self.completions = self
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        value = {"response_zh": "我也想见你。"}
        if self.calls == 1:
            value["visual"] = {"pose_name": "侧身手扶胸前站姿", "expression_name": "沉静·低落"}
        assert self.calls <= 1, "持久化候选恢复后不应再次请求模型"
        return SimpleNamespace(id=f"request_{self.calls}", usage=None, choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps(value, ensure_ascii=False)))])


class Counter:
    tokenizer_version = "test"
    def count_messages(self, messages):
        return 10


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [None, "reply", "scene_examples", "anchor_position"])
async def test_single_stage_candidate_is_persisted_and_adopted(tmp_path, corrupt):
    database = SQLiteDatabase(tmp_path / "chat.db")
    try:
        await database.initialize()
        async with database.transaction() as session:
            session.add(CharacterRow(character_id="character_deepseek", name="deepseek", character_setting="陪伴"))
        chat, repository = SQLiteChatRepository(database), SQLiteGenerationRepository(database)
        saved = await chat.accept_message(SaveMessageInput("request_001", "conv", "character_deepseek", "想见你了"))
        start = await repository.claim_job(saved.job_id)
        boundary = start.input_boundary
        prompt = FrozenPrompt(
            messages=(PromptMessage("system", '输出response_zh和visual'), PromptMessage("user", "想见你了")),
            token_count=None, context_manifest=ChatContextManifest(None, boundary.complete_turns, boundary.current_turn, None),
            memory_manifest=MemoryManifest(), character_card_version="card", tokenizer_version="test",
            tone_prompt="只润色语气，输出response_zh。", character_id="character_deepseek",
        )
        assert await repository.freeze_prompt(GenerationFreezeTicket(saved.job_id, start.attempt_id,
            saved.turn_id, boundary.session_id, start.input_version, start.active_token, prompt))
        client = Client()
        generator = GenerationReplyService(ReplyModelRepository(client))
        result = await generator.generate(prompt, expected_prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages))
        candidate = result.candidate
        ticket = ReplyAcceptanceTicket(start.attempt_id, start.input_version, start.active_token,
                                       candidate.prompt_hash, candidate.candidate_hash)
        assert await repository.save_candidate(ticket, candidate)
        restored = await repository.get_attempt(start.attempt_id)
        assert restored.candidate.generation_trace["reply"] == "我也想见你。"
        assert restored.candidate.generation_trace["request_id"] == "request_1"
        assert restored.candidate.reply.visual.pose_name.pose_name == "侧身手扶胸前站姿"
        # 此时仅有用户消息；草稿、召回、最终候选均不是正式聊天。
        async with database.db_session() as session:
            assert len((await session.scalars(select(MessageRow))).all()) == 1
        if corrupt is not None:
            async with database.transaction() as session:
                row = await session.get(GenerationAttemptRow, start.attempt_id)
                if corrupt == "reply":
                    payload = dict(row.candidate_json)
                    payload["generation_trace"] = dict(payload["generation_trace"], reply="被破坏的回复")
                    row.candidate_json = payload
                elif corrupt == "scene_examples":
                    row.frozen_prompt_json = dict(row.frozen_prompt_json, scene_examples=[{"scene_id": "tampered"}])
                else:
                    row.frozen_prompt_json = dict(row.frozen_prompt_json, anchor_position="before_user")
            with pytest.raises(InvalidStateError):
                await GenerationCompletionService(repository, generator, Counter()).complete(start.attempt_id)
            async with database.db_session() as session:
                assert len((await session.scalars(select(MessageRow))).all()) == 1
            return
        accepted = await GenerationCompletionService(repository, generator, Counter()).complete(start.attempt_id)
        assert accepted.content == "我也想见你。"
        assert client.calls == 1
        async with database.db_session() as session:
            messages = (await session.scalars(select(MessageRow).order_by(MessageRow.message_seq))).all()
            assert [(item.role, item.content) for item in messages] == [("user", "想见你了"), ("character", "我也想见你。")]
            row = await session.get(GenerationAttemptRow, start.attempt_id)
            assert row.candidate_json["generation_trace"]["schema"] == "single-stage-scene-reply-v1"
            assert messages[1].visual_json == row.candidate_json["generation_trace"]["reply_visual"]
    finally:
        await database.close()
