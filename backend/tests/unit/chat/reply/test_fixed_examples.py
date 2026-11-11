"""固定示例拆分保留原文、分组与边界，不进入当前输入或真实历史预算。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.character.core.domain.models import CharacterCard
from module.shared.context.types.working import WorkingContext, CurrentTurnInput
from module.chat.reply.core.domain.config.context_budget import ContextPromptBudget
from module.chat.reply.core.domain.errors.preparation import PromptMaterialError, PromptBudgetExceeded
from module.chat.reply.core.processing.prompt_builder import PromptBuilder, REPLY_ANCHOR
from module.chat.reply.core.domain.materials import GenerationInputBoundary, ReplyPromptResources
from module.memory_recall.agent.core.domain.summary import MemorySummary


EXAMPLE = '{"response_zh":"有异议。","visual":{"pose_name":"正面自然站姿","expression_name":"挖苦·调侃"}}'
CARD = ("## 语言指纹\n自然接话。\n\n## 固定示例\n作者的示例说明。\n"
        "### 示例1：较劲\n｛｛user｝｝： 用户原句。\ndeepseek： " + EXAMPLE + "\n"
        "### 示例2：独立场景\n｛｛user｝｝： 第二个用户原句。\ndeepseek： " + EXAMPLE + "\n"
        "## 每轮生成锚点\n作者的原锚点。\n")


def test_actual_character_examples_are_preserved_verbatim():
    path = Path(__file__).resolve().parents[4] / "resources/characters/character_deepseek/角色卡.md"
    original = path.read_text(encoding="utf-8")
    stripped, messages = PromptBuilder.split_fixed_examples(original, "deepseek")
    users = [line.split("：", 1)[1].strip() for line in original.splitlines() if line.startswith("｛｛user｝｝：")]
    replies = [line.split("：", 1)[1].strip() for line in original.splitlines() if line.startswith("deepseek：")]
    assert [m.content for m in messages if m.role == "user"] == users
    assert [m.content for m in messages if m.role == "assistant"] == replies
    assert len(users) == len(replies) and users
    assert "## 语言指纹" in stripped and "## 每轮生成锚点" in stripped
    assert "### 示例1" not in stripped
    assert messages[-1].role == "system" and "固定示例结束" in messages[-1].content


def test_groups_remain_independent_and_examples_are_not_duplicated():
    stripped, messages = PromptBuilder.split_fixed_examples(CARD, "deepseek")
    assert [(m.role, m.content) for m in messages if m.role != "system"] == [
        ("user", "用户原句。"), ("assistant", EXAMPLE),
        ("user", "第二个用户原句。"), ("assistant", EXAMPLE),
    ]
    assert sum("独立固定示例" in m.content for m in messages) == 2
    assert "作者的示例说明。" in stripped and "作者的原锚点。" in stripped
    assert EXAMPLE not in stripped


@pytest.mark.parametrize("bad", [
    CARD.replace("deepseek： " + EXAMPLE, "deepseek： 不是JSON", 1),
    CARD.replace("deepseek： " + EXAMPLE, "", 1),
    CARD.replace("｛｛user｝｝： 用户原句。", "", 1),
    "## 固定示例\n只有说明，没有示范。\n",
])
def test_malformed_examples_fail_instead_of_being_silently_dropped(bad):
    with pytest.raises(PromptMaterialError):
        PromptBuilder.split_fixed_examples(bad, "deepseek")


def test_cards_without_fixed_examples_are_unchanged():
    assert PromptBuilder.split_fixed_examples("只有角色设定", "deepseek") == ("只有角色设定", ())


def materials():
    boundary = GenerationInputBoundary.fix(conversation_id="conv", character_id="character_deepseek", session_id="session",
        turn_id="turn", turn_seq=1, input_version=1, message_ids=("u1", "u2"))
    context = WorkingContext(None, (), CurrentTurnInput(boundary.current_turn,
        (SimpleNamespace(content="真实前半句"), SimpleNamespace(content="真实最后一句"))), None)
    resources = ReplyPromptResources(CharacterCard("character_deepseek", "deepseek", CARD, Path(".")),
                                    "card-v1", "系统身份", "输出JSON", "template-v1")
    return dict(boundary=boundary, context=context, resources=resources,
                memory=MemorySummary("empty", "", "", ()))


def test_example_messages_are_separated_from_actual_input_and_anchor():
    values = materials()
    prompt = PromptBuilder(example_format="dialogue").build(**values)
    assert [m.content for m in prompt.messages[-2:]] == ["真实前半句", "真实最后一句"]
    assert prompt.messages[-3].content == REPLY_ANCHOR
    assert prompt.context_manifest.current_turn == values["boundary"].current_turn
    assert len([m for m in prompt.messages if m.role == "assistant"]) == 2
    assert prompt.messages[0].content.count(EXAMPLE) == 0
    assert "固定示例结束" in prompt.messages[-4].content


def test_examples_use_character_budget_instead_of_real_history_budget():
    class Counter:
        tokenizer_version = "test"

        def count_messages(self, messages):
            return sum(len(m.content) for m in messages)

    values = materials()
    budget = replace(ContextPromptBudget(), history_budget=1)
    assert PromptBuilder(Counter(), budget, example_format="dialogue").build(**values).budget_checked
    with pytest.raises(PromptBudgetExceeded, match="character_card"):
        PromptBuilder(Counter(), replace(budget, character_card_budget=20), example_format="dialogue").build(**values)


def test_dialogue_is_default_and_embedded_remains_configurable(monkeypatch):
    from storage.config.settings import Settings

    monkeypatch.delenv("RNA_REPLY_EXAMPLE_FORMAT", raising=False)
    assert Settings(_env_file=None).reply_example_format == "dialogue"
    values = materials()
    default = PromptBuilder().build(**values)
    assert [m.content for m in default.messages if m.role == "assistant"] == [EXAMPLE, EXAMPLE]
    embedded = PromptBuilder(example_format="embedded").build(**values)
    assert not any(m.role == "assistant" for m in embedded.messages)
    assert EXAMPLE in embedded.messages[0].content
