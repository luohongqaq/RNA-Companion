"""单次读取账本只依赖材料契约，缓存材料与不同 attempt 的账本互不覆盖。"""
from types import SimpleNamespace

from module.memory_recall.retrieval.core.processing.session import MemoryReadSession


def test_sessions_restore_independent_records_from_minimal_seed():
    record = dict(document_id="daily-1", kind="daily", title="旅行计划", revision=1,
        content_hash="hash-1", sections={"intentions": [{"text": "下周出发"}]},
        conversation_time=None, time_context={})
    seed = SimpleNamespace(recent_update_memory=[record], recent_retrievals=[])
    first = MemoryReadSession(None, seed)
    second = MemoryReadSession(None, seed)
    assert first.cards == second.cards == {"card_001": "daily-1"}
    first.read_set["read_001"]["sections"]["intentions"][0]["text"] = "本轮变化"
    assert record["sections"]["intentions"][0]["text"] == "下周出发"
    assert second.read_set["read_001"]["sections"]["intentions"][0]["text"] == "下周出发"
    first.register_candidate("topic-2")
    assert second.cards == {"card_001": "daily-1"}
