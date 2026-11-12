"""单阶段仅调用一次回复模型，解析台词和visual并保留冻结证据。"""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from pathlib import Path
import pytest
from module.shared.context.types.visual import Expression, Pose, VisualState
from module.chat.reply.core.domain.errors.reply import ReplyOutputError, ReplyTruncatedError
from module.shared.character.types.manifest import CharacterResourceManifest, VisualOptions
from module.shared.character.card_render import CharacterCardRenderer
from module.chat.reply.core.processing.prompt_builder import PromptBuilder
from module.chat.reply.core.domain.config.reply import ReplyModelConfig
from module.chat.reply.core.domain.fingerprint import ReplyFingerprint
from module.chat.reply.core.processing.service import GenerationReplyService
from module.chat.reply.core.domain.materials import ChatContextManifest, FrozenPrompt, GenerationInputBoundary, MemoryManifest, PromptMessage
from module.chat.scenes_recall.core.domain.models import SceneExample
from module.chat.reply.repository.llm import ReplyModelRepository


class ScriptedClient:
    def __init__(self, outputs):
        self.chat = self.completions = self
        self.outputs, self.calls = list(outputs), []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content, finish = self.outputs.pop(0)
        return SimpleNamespace(id=f"call_{len(self.calls)}", usage=None, choices=[SimpleNamespace(
            finish_reason=finish, message=SimpleNamespace(content=content))])


class FakeScenes:
    def __init__(self, hits=()):
        self.hits, self.queries = hits, []

    async def search(self, query):
        self.queries.append(query)
        return self.hits


@pytest.fixture
def visual_options():
    """合成选项：两种姿势、多个表情，验证配对不受限制。"""
    return VisualOptions(
        ("正面自然站姿", "侧身手扶胸前站姿"),
        ("微笑·温和", "傻笑·得意", "傲娇·羞恼", "喔口·领会/玩闹", "闭眼大笑·喜悦", "沉静·低落", "悲伤·泪目"),
    )


@pytest.fixture
def prompt(visual_options):
    boundary = GenerationInputBoundary.fix(conversation_id="conv", character_id="character_deepseek", session_id="session",
        turn_id="turn", turn_seq=2, input_version=1, message_ids=("msg",))
    return FrozenPrompt(
        messages=(PromptMessage("system", "角色身份和合法动作"),
                  PromptMessage("user", "【参考资料：长期记忆】\n不能当成对话查询"),
                  PromptMessage("user", "最近怎么样"), PromptMessage("assistant", "还好"),
                  PromptMessage("user", "想见你了")),
        token_count=None, context_manifest=ChatContextManifest(None, boundary.complete_turns, boundary.current_turn, None),
        memory_manifest=MemoryManifest(), character_card_version="card", tokenizer_version="test",
        tone_prompt="只润色台词，输出response_zh。", character_id="character_deepseek",
        current_visual=VisualState(Pose("old", "正面自然站姿"), Expression("old", "微笑·温和")),
        visual_options=visual_options,
    )


def draft(text="很想你。", pose="侧身手扶胸前站姿", expression="沉静·低落"):
    return json.dumps({"response_zh": text, "visual": {"pose_name": pose, "expression_name": expression}}, ensure_ascii=False)


def polished(text="我也是，能再和你聊聊真好。"):
    return json.dumps({"response_zh": text}, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_scenes", [False, True])
async def test_single_request_uses_frozen_prompt_and_preserves_visual(prompt, with_scenes):
    if with_scenes:
        prompt = replace(prompt, scene_examples=({"scene_id": "s1", "context": ["情境"], "reply": "参考", "score": .9},))
    client = ScriptedClient([(draft(), "stop")])
    result = await GenerationReplyService(ReplyModelRepository(client)).generate(prompt, expected_prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages))
    assert len(client.calls) == 1
    assert client.calls[0]["messages"] == [{"role": m.role, "content": m.content} for m in prompt.messages]
    assert result.candidate.reply.response_zh == "很想你。"
    assert result.candidate.reply.visual.pose_name.pose_name == "侧身手扶胸前站姿"
    trace = result.candidate.generation_trace
    assert trace["schema"] == "single-stage-scene-reply-v1"
    assert trace["scenes"] == list(prompt.scene_examples)
    assert trace["reply_visual"]["pose_name"]["pose_name"] == "侧身手扶胸前站姿"
    assert trace["request_id"] == "call_1"
    assert "draft" not in trace and "polish_request_id" not in trace


@pytest.mark.asyncio
@pytest.mark.parametrize("output,finish", [
    ('{"response_zh":"缺少动作"}', "stop"),
    ("只有台词，没有动作", "stop"),
    ('{"response_zh":', "stop"),
    (draft(pose="不存在的姿势"), "stop"),
    (draft(expression="不存在的表情"), "stop"),
    (draft(), "length"),
])
async def test_invalid_output_does_not_trigger_another_model_call(prompt, output, finish):
    client = ScriptedClient([(output, finish)])
    with pytest.raises(ReplyOutputError):
        await GenerationReplyService(ReplyModelRepository(client)).generate(prompt, expected_prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages))
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_complete_fenced_json_is_supported(prompt):
    client = ScriptedClient([('```json\n' + draft() + '\n```', "stop")])
    result = await GenerationReplyService(ReplyModelRepository(client)).generate(prompt, expected_prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages))
    assert result.candidate.reply.response_zh == "很想你。"


@pytest.mark.asyncio
async def test_obsolete_response_is_discarded(prompt):
    checks = iter((True, False))
    client = ScriptedClient([(draft(), "stop")])
    with pytest.raises(asyncio.CancelledError):
        await GenerationReplyService(ReplyModelRepository(client)).generate(prompt,
                                                      expected_prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages), is_current=lambda: next(checks))
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["glm-5.3-flash", "glm-5.3-flashx"])
async def test_glm53_uses_low_reasoning_without_disabled_thinking(prompt, model):
    client = ScriptedClient([(draft(), "stop")])
    config = ReplyModelConfig(model_name=model, reasoning_effort="low")
    await GenerationReplyService(ReplyModelRepository(client), config).generate(prompt, expected_prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages))
    assert len(client.calls) == 1
    assert client.calls[0]["reasoning_effort"] == "low"
    assert "extra_body" not in client.calls[0]
    assert client.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
@pytest.mark.parametrize("pose,expression", [
    ("侧身手扶胸前站姿", "傻笑·得意"),
    ("正面自然站姿", "傲娇·羞恼"),
    ("正面自然站姿", "喔口·领会/玩闹"),
    ("侧身手扶胸前站姿", "闭眼大笑·喜悦"),
])
async def test_allowed_expression_is_accepted_without_pair_restrictions(prompt, pose, expression):
    client = ScriptedClient([(draft(pose=pose, expression=expression), "stop")])
    result = await GenerationReplyService(ReplyModelRepository(client)).generate(prompt, expected_prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages))
    assert result.candidate.reply.visual.expression_name.expression_name == expression
    assert result.candidate.reply.visual.pose_name.pose_name == pose
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_allowed_expressions_can_repeat_and_switch_in_any_order(prompt):
    sequence = ("傻笑·得意", "傻笑·得意", "悲伤·泪目", "傻笑·得意")
    client = ScriptedClient([(draft(expression=name), "stop") for name in sequence])
    service = GenerationReplyService(ReplyModelRepository(client))
    for name in sequence:
        result = await service.generate(prompt, expected_prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages))
        assert result.candidate.reply.visual.expression_name.expression_name == name
        prompt = replace(prompt, current_visual=result.candidate.reply.visual)
    assert len(client.calls) == len(sequence)


@pytest.mark.asyncio
async def test_reply_uses_frozen_character_visual_options(prompt):
    from module.chat.reply.repository.mappers import _frozen_prompt_from_json, _frozen_prompt_to_json

    prompt = replace(prompt, visual_options=VisualOptions(("挥手",), ("惊喜",)))
    prompt = _frozen_prompt_from_json(_frozen_prompt_to_json(prompt))
    client = ScriptedClient([(draft(pose="挥手", expression="惊喜"), "stop"), (draft(), "stop")])
    service = GenerationReplyService(ReplyModelRepository(client))
    expected_hash = ReplyFingerprint.prompt_hash(prompt.messages)
    result = await service.generate(prompt, expected_prompt_hash=expected_hash)
    assert result.candidate.reply.visual.names() == {"pose_name": "挥手", "expression_name": "惊喜"}
    with pytest.raises(ReplyOutputError):
        await service.generate(prompt, expected_prompt_hash=expected_hash)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "嗯……我大概懂那种别扭。喜欢的角色是",
    "原来是这样……那这个愿望，我记下了。不过主人，能把",
    "哦——RAG算是上岸啦！从被调参折磨成那样到",
])
async def test_reply_stopping_mid_sentence_is_truncated(prompt, text):
    """完整JSON也可能停在半句；这种候选不能当作说完的话保存。"""
    client = ScriptedClient([(draft(text=text), "stop")])
    with pytest.raises(ReplyTruncatedError):
        await GenerationReplyService(ReplyModelRepository(client)).generate(
            prompt, expected_prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages))
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "嗯，今晚早点睡吧～", "就这样定啦。", "……你认真的？", "好呀", "我知道了，那明天见！", "行，那就这样……",
])
async def test_reply_ending_naturally_is_not_truncated(prompt, text):
    """句末没有标点但语义说完的短回复不能误判。"""
    client = ScriptedClient([(draft(text=text), "stop")])
    result = await GenerationReplyService(ReplyModelRepository(client)).generate(
        prompt, expected_prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages))
    assert result.candidate.reply.response_zh == text


def test_all_authored_fixed_example_visuals_pass_validation():
    directory = Path(__file__).resolve().parents[4] / "resources/characters/character_deepseek"
    manifest = CharacterResourceManifest.model_validate_json((directory / "manifest.json").read_bytes())
    options = manifest.expression.visual_options()
    generator = GenerationReplyService(ReplyModelRepository(None))
    examples = [line.split("：", 1)[1].strip() for line in (directory / "角色卡.md").read_text(encoding="utf-8").splitlines()
                if line.startswith("deepseek：")]
    assert examples
    for raw in examples:
        GenerationReplyService.validate_reply_visual(generator.parse_character_reply(raw), options)


def test_rendered_fixed_example_preserves_appellation_with_json_special_characters():
    card = (
        "## 身份\n\n测试角色把{{ user_name }}视作伙伴。\n\n"
        "## 固定示例\n固定示例不是当前聊天事实。\n"
        "### 示例1：日常应答\n用户：在忙吗？\n"
        '测试角色： {"response_zh":"在的，{{ user_name | json_string }}叫我。","visual":{"pose_name":"正面自然站姿","expression_name":"微笑·温和"}}\n'
    )
    appellation = '小"林\\同学'
    rendered = CharacterCardRenderer().render(card, None, appellation)
    content, examples = PromptBuilder.split_fixed_examples(rendered, "测试角色")
    assert appellation in content
    assert any(appellation in json.loads(example.content)["response_zh"]
               for example in examples if example.role == "assistant")


@pytest.mark.asyncio
async def test_visual_json_key_order_is_not_restricted(prompt):
    content = json.dumps({"visual": {"expression_name": "傻笑·得意", "pose_name": "侧身手扶胸前站姿"},
                          "response_zh": "嘿嘿。"}, ensure_ascii=False)
    client = ScriptedClient([(content, "stop")])
    result = await GenerationReplyService(ReplyModelRepository(client)).generate(prompt, expected_prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages))
    assert result.candidate.reply.response_zh == "嘿嘿。"


@pytest.mark.asyncio
async def test_provider_usage_survives_candidate_persistence(prompt):
    from module.chat.reply.core.domain.materials import ModelUsage
    from module.chat.reply.repository import mappers as reply_mappers

    client = ScriptedClient([(draft(), "stop")])
    original_create = client.create
    async def create_with_usage(**kwargs):
        response = await original_create(**kwargs)
        response.usage = SimpleNamespace(prompt_tokens=1200, completion_tokens=300, total_tokens=1500)
        return response
    client.create = create_with_usage
    result = await GenerationReplyService(ReplyModelRepository(client)).generate(prompt, expected_prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages))
    expected = ModelUsage(1200, 300, 1500)
    assert result.candidate.usage == expected
    restored = reply_mappers._candidate_from_json(reply_mappers._candidate_to_json(result.candidate))
    assert restored.usage == expected
