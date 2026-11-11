from module.memory_recall.agent.core.domain.summary_input import MemorySummaryInput


def test_summary_receives_unresolved_without_treating_it_as_memory():
    selection = {"selected": [], "unresolved": ["未确认这次庆祝是否对应此前奖学金"],
                 "trajectory": [{"reasoning_content": "不应进入总结的模型内部日志"}]}
    payload = MemorySummaryInput.build(selection, "这次值得庆祝", [])
    assert payload["SELECTION_UNRESOLVED"] == selection["unresolved"]
    assert payload["MEMORIES"] == ""
    assert payload["CURRENT_INPUT"] == "这次值得庆祝"
    assert "不应进入总结的模型内部日志" not in str(payload)
    payload["SELECTION_UNRESOLVED"].append("新增项")
    assert len(selection["unresolved"]) == 1


def test_current_update_and_historical_card_remain_separate():
    selection = {"selected": [{"kind": "daily", "title": "此前面试",
        "sections": {"memorable_moments": "用户仍在等待录用通知"},
        "source": {"conversation_time": "2026-09-24T10:00:00+08:00"}}]}
    payload = MemorySummaryInput.build(selection, "今天拿到offer了", [])
    assert "仍在等待录用通知" in payload["MEMORIES"]
    assert "今天拿到offer了" not in payload["MEMORIES"]
    assert payload["CURRENT_INPUT"] == "今天拿到offer了"
    assert payload["SELECTION_UNRESOLVED"] == []
