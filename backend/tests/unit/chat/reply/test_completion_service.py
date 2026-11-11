"""T3 单元测试：验证候选回复的采用编排。"""

from datetime import datetime, timezone

import pytest

from module.character.core.domain.models import CharacterReply
from module.shared.context.types.visual import Expression, Pose, VisualState
from module.chat.conversation.core.domain.contracts import AcceptedReply
from module.chat.reply.core.processing.completion import GenerationCompletionService
from module.chat.reply.core.domain.fingerprint import ReplyFingerprint
from module.chat.reply.core.domain.materials import (
    ChatContextManifest,
    FrozenPrompt,
    GenerationInputBoundary,
    MemoryManifest,
    PromptMessage,
    ReplyCandidate,
)
from module.chat.reply.core.domain.models import (
    AttemptState,
    GenerationAttempt,
)


class FakeGenerationRepository:
    """模拟仓储，只记录调用和返回结果，不操作数据库。"""

    def __init__(self):
        self.attempt = None
        self.accepted_reply = None
        self.accept_error = None

        self.read_attempt_ids = []
        self.accept_calls = []
        self.save_candidate_calls = []

    async def get_attempt(self, attempt_id):
        self.read_attempt_ids.append(attempt_id)
        return self.attempt

    async def save_candidate(self, ticket, candidate):
        self.save_candidate_calls.append((ticket, candidate))
        return True

    async def accept_reply(
        self,
        ticket,
        *,
        turn_token_count,
        tokenizer_version,
    ):
        self.accept_calls.append(
            (ticket, turn_token_count, tokenizer_version)
        )

        if self.accept_error is not None:
            raise self.accept_error

        return self.accepted_reply


class FakeReplyGeneration:
    """T3 已有候选，不应该再次调用模型。"""

    def __init__(self):
        self.calls = []

    async def generate(self, prompt, *, expected_prompt_hash):
        self.calls.append((prompt, expected_prompt_hash))
        raise AssertionError("已有候选时不应该重新调用模型")


class FakeTokenCounter:
    """固定返回 20，检查服务是否正确组织计数输入。"""

    tokenizer_version = "test-tokenizer"

    def __init__(self):
        self.calls = []

    def count_messages(self, messages):
        self.calls.append(tuple(messages))
        return 20


@pytest.fixture
def repository():
    return FakeGenerationRepository()


@pytest.fixture
def reply_generation():
    return FakeReplyGeneration()


@pytest.fixture
def token_counter():
    return FakeTokenCounter()


@pytest.fixture
def attempt():
    """准备一份具有冻结提示词、持久候选的业务对象。"""
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)

    # 当前轮有两条用户消息。
    boundary = GenerationInputBoundary.fix(
        conversation_id="conversation_001",
        character_id="character_test",
        session_id="session_001",
        turn_id="turn_001",
        turn_seq=1,
        input_version=2,
        message_ids=("user_001", "user_002"),
    )

    prompt = FrozenPrompt(
        messages=(
            PromptMessage("system", "你是一个桌宠。"),
            PromptMessage("user", "今天有点累"),
            PromptMessage("user", "想休息一下"),
        ),
        token_count=None,
        context_manifest=ChatContextManifest(
            working_snapshot=boundary.working_snapshot,
            complete_turns=boundary.complete_turns,
            current_turn=boundary.current_turn,
            inherited_context=boundary.inherited_context,
        ),
        memory_manifest=MemoryManifest(),
        character_card_version="test-card",
        tokenizer_version="unconfigured",
    )

    reply = CharacterReply(
        response_zh="那就先休息一下吧。",
        visual=VisualState(
            pose_name=Pose("pose_001", "standing"),
            expression_name=Expression("expression_001", "smile"),
        ),
    )

    candidate = ReplyCandidate(
        reply=reply,
        candidate_hash=ReplyFingerprint.candidate_hash(reply),
        prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages),
        usage=None,
        created_at=now,
    )

    return GenerationAttempt(
        attempt_id="attempt_001",
        job_id="job_001",
        turn_id="turn_001",
        attempt_seq=1,
        input_version=2,
        active_token=3,
        state=AttemptState.GENERATING,
        input_boundary=boundary,
        created_at=now,
        updated_at=now,
        frozen_prompt=prompt,
        candidate=candidate,
    )


@pytest.fixture
def accepted_reply(attempt):
    return AcceptedReply(
        conversation_id="conversation_001",
        turn_id="turn_001",
        message_id="reply_001",
        content=attempt.candidate.reply.response_zh,
        visual=attempt.candidate.reply.visual,
        created_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_missing_attempt_does_not_accept_reply(
    repository,
    reply_generation,
    token_counter,
):
    """验证在没有找到 attempt 时，不应该进行采用。"""
    # 1. 准备：仓储查不到 attempt。
    repository.attempt = None

    service = GenerationCompletionService(
        repository,
        reply_generation,
        token_counter,
    )

    # 2. 执行。
    result = await service.complete("missing_attempt")

    # 3. 检查：没有后续生成、计数和采用。
    assert result is None
    assert repository.read_attempt_ids == ["missing_attempt"]
    assert repository.accept_calls == []
    assert repository.save_candidate_calls == []
    assert reply_generation.calls == []
    assert token_counter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        AttemptState.RETRIEVING,
        AttemptState.SUPERSEDED,
        AttemptState.FAILED,
    ],
)
async def test_ineligible_attempt_does_not_accept_reply(
    repository,
    reply_generation,
    token_counter,
    attempt,
    state,
):
    """验证在 attempt 状态不允许采用时，不应该进行采用。"""
    # 1. 准备：attempt 存在，但状态不允许采用。
    attempt.state = state
    repository.attempt = attempt

    service = GenerationCompletionService(
        repository,
        reply_generation,
        token_counter,
    )

    # 2. 执行。
    result = await service.complete(attempt.attempt_id)

    # 3. 检查：直接停止。
    assert result is None
    assert repository.accept_calls == []
    assert repository.save_candidate_calls == []
    assert reply_generation.calls == []
    assert token_counter.calls == []


@pytest.mark.asyncio
async def test_saved_candidate_is_passed_to_adoption(
    repository,
    reply_generation,
    token_counter,
    attempt,
    accepted_reply,
):
    """验证在有保存候选时，不应该重新生成、计数或保存候选。"""
    # 1. 准备：已有候选，仓储采用后返回正式回复。
    repository.attempt = attempt
    repository.accepted_reply = accepted_reply

    service = GenerationCompletionService(
        repository,
        reply_generation,
        token_counter,
    )

    # 2. 执行。
    result = await service.complete(attempt.attempt_id)

    # 3. 检查：返回仓储确认的正式回复。
    assert result == accepted_reply

    # 已有候选，不重新调用模型，也不重新保存候选。
    assert reply_generation.calls == []
    assert repository.save_candidate_calls == []

    # 仓储只收到一次采用请求。
    assert len(repository.accept_calls) == 1

    ticket, count, version = repository.accept_calls[0]

    # 检查采用资格和材料指纹是否正确传递。
    assert ticket.attempt_id == attempt.attempt_id
    assert ticket.input_version == 2
    assert ticket.active_token == 3
    assert ticket.prompt_hash == attempt.candidate.prompt_hash
    assert ticket.candidate_hash == attempt.candidate.candidate_hash

    # 检查计数器结果和版本是否正确传递。
    assert count == 20
    assert version == "test-tokenizer"


@pytest.mark.asyncio
async def test_token_count_contains_only_current_input_and_reply(
    repository,
    reply_generation,
    token_counter,
    attempt,
    accepted_reply,
):
    # 1. 准备。
    repository.attempt = attempt
    repository.accepted_reply = accepted_reply

    service = GenerationCompletionService(
        repository,
        reply_generation,
        token_counter,
    )

    # 2. 执行。
    await service.complete(attempt.attempt_id)

    # 3. 检查计数输入：
    # 包含两条当前用户输入和回复，不包含 system 提示词。
    assert len(token_counter.calls) == 1
    assert token_counter.calls[0] == (
        PromptMessage("user", "今天有点累"),
        PromptMessage("user", "想休息一下"),
        PromptMessage("assistant", "那就先休息一下吧。"),
    )


@pytest.mark.asyncio
async def test_rejected_adoption_returns_none(
    repository,
    reply_generation,
    token_counter,
    attempt,
):
    # 1. 准备：仓储拒绝采用，返回 None。
    repository.attempt = attempt
    repository.accepted_reply = None

    service = GenerationCompletionService(
        repository,
        reply_generation,
        token_counter,
    )

    # 2. 执行。
    result = await service.complete(attempt.attempt_id)

    # 3. 检查：不能把未采用的候选当成正式回复。
    assert result is None
    assert len(repository.accept_calls) == 1
    assert reply_generation.calls == []


@pytest.mark.asyncio
async def test_adoption_failure_propagates(
    repository,
    reply_generation,
    token_counter,
    attempt,
):
    # 1. 准备：仓储采用时失败。
    repository.attempt = attempt
    error = RuntimeError("模拟采用失败")
    repository.accept_error = error

    service = GenerationCompletionService(
        repository,
        reply_generation,
        token_counter,
    )

    # 2. 执行并检查异常。
    with pytest.raises(RuntimeError) as caught:
        await service.complete(attempt.attempt_id)

    # 3. 检查：原异常向上传播，不返回伪造的成功结果。
    assert caught.value is error
    assert len(repository.accept_calls) == 1
    assert reply_generation.calls == []

@pytest.mark.asyncio
async def test_provider_usage_is_not_added_to_local_history_count(
    repository, reply_generation, token_counter, attempt, accepted_reply,
):
    from dataclasses import replace
    from module.chat.reply.core.domain.materials import ModelUsage
    repository.attempt = replace(attempt, candidate=replace(
        attempt.candidate, usage=ModelUsage(1200, 300, 1500),
    ))
    repository.accepted_reply = accepted_reply
    await GenerationCompletionService(repository, reply_generation, token_counter).complete(attempt.attempt_id)
    _, count, version = repository.accept_calls[0]
    assert count == 20
    assert version == "test-tokenizer"
    assert repository.attempt.candidate.usage.output_tokens == 300


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "rejected", "error", "notify_error"])
async def test_context_notification_only_after_successful_t3(
    repository, reply_generation, token_counter, attempt, accepted_reply, outcome,
):
    repository.attempt = attempt
    repository.accepted_reply = None if outcome == "rejected" else accepted_reply
    notified = []

    def notify():
        assert len(repository.accept_calls) == 1
        notified.append(True)
        if outcome == "notify_error":
            raise RuntimeError("notification failed")

    if outcome == "error":
        repository.accept_error = RuntimeError("transaction failed")
    service = GenerationCompletionService(repository, reply_generation, token_counter, notify_context=notify)
    if outcome == "error":
        with pytest.raises(RuntimeError, match="transaction failed"):
            await service.complete(attempt.attempt_id)
    else:
        result = await service.complete(attempt.attempt_id)
        assert result == repository.accepted_reply
    assert bool(notified) == (outcome in ("accepted", "notify_error"))
