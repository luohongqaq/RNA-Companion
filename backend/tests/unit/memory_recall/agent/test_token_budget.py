import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.memory_recall.agent.core.domain.policy import Budget
from module.memory_recall.agent.core.domain.state import AttemptState
from module.memory_recall.agent.core.processing.tools.executor import ToolExecutor
from module.memory_recall.retrieval.core.processing.service import MemoryReadService
from module.memory_recall.shared.core.domain.budgeting import RecallMaterialBudget
from storage.llm.token_counter import TokenizerTokenCounter


@pytest.fixture(scope="module")
def counter():
    result = TokenizerTokenCounter()
    result.load_directory(Path(__file__).resolve().parents[4] / "resources" / "deepseek_v4_tokenizer")
    return result


def test_material_and_document_use_same_real_tokenizer(counter):
    value = {"内容": "RNA、DeepSeek 和中文测试 123! 🧬"}
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    expected = len(counter._tokenizer.encode(serialized, add_special_tokens=False))
    assert RecallMaterialBudget.count_tokens(value, counter) == expected
    reader = MemoryReadService(None, None, "character", counter)
    assert reader._tokens(value) == expected


def test_tool_budget_trims_using_actual_tokens(counter):
    items = [{"title": "中文和 English mixed 🧬" * 8}, {"title": "another 文档" * 30}]
    one = {"status": "ok", "items": items[:1]}
    two = {"status": "ok", "items": items}
    assert RecallMaterialBudget.count_tokens(two, counter) > RecallMaterialBudget.count_tokens(one, counter)
    state = AttemptState(budget=Budget(output_token_limit=RecallMaterialBudget.count_tokens(two, counter) - 1, error_reserve=0))
    executor = ToolExecutor(SimpleNamespace(), state, counter)
    result = executor._fit_output(two)
    assert result["status"] == "partial"
    assert len(result.get("items", [])) < 2
    assert RecallMaterialBudget.count_tokens(result, counter) <= state.budget.output_token_limit
