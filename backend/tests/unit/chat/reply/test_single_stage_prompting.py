"""锚点位置、动态场景和连续用户输入的冻结约束。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.character.core.domain.models import CharacterCard
from module.shared.context.types.working import WorkingContext, CurrentTurnInput
from module.chat.reply.core.domain.errors.preparation import PromptBudgetExceeded
from module.chat.reply.core.domain.config.context_budget import ContextPromptBudget
from module.chat.reply.core.processing.prompt_builder import PromptBuilder
from module.chat.reply.core.processing.prompt_builder import REPLY_ANCHOR
from module.chat.reply.core.domain.materials import GenerationInputBoundary, ReplyPromptResources
from module.chat.scenes_recall.core.domain.models import SceneExample
from module.memory_recall.agent.core.domain.summary import MemorySummary


@pytest.fixture
def materials():
    boundary = GenerationInputBoundary.fix(conversation_id="conv", character_id="character_deepseek", session_id="session",
        turn_id="turn", turn_seq=1, input_version=1, message_ids=("u1", "u2"))
    context = WorkingContext(None, (), CurrentTurnInput(boundary.current_turn,
        (SimpleNamespace(content="前半句"), SimpleNamespace(content="最后一句"))), None)
    resources = ReplyPromptResources(CharacterCard("character_deepseek", "deepseek", "语言指纹和固定示例", Path(".")),
                                    "card-v1", "系统身份", "输出JSON", "template-v1", "旧润色提示词")
    scenes = (SceneExample("s1", "session", .9, ("情境",), "参考对话", "hash"),)
    return dict(boundary=boundary, context=context, resources=resources,
                memory=MemorySummary("empty", "", "", ()), scenes=scenes)


@pytest.mark.parametrize("position", ["system_end", "before_user"])
def test_anchor_and_scenes_are_in_final_model_input(materials, position):
    prompt = PromptBuilder(anchor_position=position).build(**materials)
    assert [m.content for m in prompt.messages[-2:]] == ["前半句", "最后一句"]
    assert sum(REPLY_ANCHOR in m.content for m in prompt.messages) == 1
    assert any("参考对话" in m.content and m.content.startswith("【参考资料：场景示例") for m in prompt.messages)
    assert all("旧润色提示词" not in m.content for m in prompt.messages)
    assert prompt.scene_examples[0]["scene_id"] == "s1"
    if position == "system_end":
        assert prompt.messages[0].content.endswith(REPLY_ANCHOR)
    else:
        assert prompt.messages[-3].role == "system"
        assert prompt.messages[-3].content == REPLY_ANCHOR
        assert REPLY_ANCHOR not in prompt.messages[0].content


def test_scene_reference_is_included_in_token_budget(materials):
    class Counter:
        tokenizer_version = "test"

        def count_messages(self, messages):
            return sum(len(m.content) for m in messages)

    budget = replace(ContextPromptBudget(), scene_reference_budget=1)
    with pytest.raises(PromptBudgetExceeded, match="scenes"):
        PromptBuilder(Counter(), budget).build(**materials)


def test_history_maintenance_target_does_not_block_reply_but_total_capacity_does(materials):
    from module.shared.context.types.references import SnapshotRef
    from module.shared.context.types.working import SnapshotContext
    from module.shared.context.types.summary import StructuredSummary
    class Counter:
        tokenizer_version = "test"
        def count_messages(self, messages):
            return sum(len(m.content) for m in messages)
    snapshot = SnapshotContext(SnapshotRef("summary", "hash", 0), StructuredSummary("history"), "x" * 3000)
    values = dict(materials, context=replace(materials["context"], working_snapshot=snapshot))
    budget = replace(ContextPromptBudget(), history_budget=2000)
    prompt = PromptBuilder(Counter(), budget).build(**values)
    assert prompt.budget_checked
    assert any("x" * 3000 in m.content for m in prompt.messages)
    with pytest.raises(PromptBudgetExceeded, match="安全余量"):
        PromptBuilder(Counter(), replace(budget, effective_context_limit=9500)).build(**values)
