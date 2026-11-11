"""工具注册表：每个读取工具的全部差异集中在一个 ToolSpec。

分工：contracts 包仍是模型侧与 Python 侧契约（三方同步不动）；本文件只把
"哪个工具用哪个输入模型、调 store 的哪个方法、结果键是什么、次数上限
多少、准入检查是什么"收拢成一条声明式记录。想看某个工具的一切，
从它的 ToolSpec 和 check 函数开始。
finish_selection 不注册：它不碰 store、拒绝语义是 rejected 而非 partial、
且会终结整个循环，由 agent.selection 单独处理。
新增读取工具 = 注册一个 ToolSpec + 一个 check 函数，agent 流水线不动。
"""
from __future__ import annotations
from module.memory_recall.agent.core.domain.state import AttemptState
from module.memory_recall.agent.core.domain.tool import ToolSpec
from module.memory_recall.retrieval.core.domain.tools.listing import ListInput
from module.memory_recall.retrieval.core.domain.tools.read import ReadInput
from module.memory_recall.retrieval.core.domain.tools.search import SearchInput
from module.memory_recall.shared.core.domain.errors import (
    FALLBACK_REQUIRES_SEARCH,
    INAPPLICABLE_FIELD,
    INVALID_SCOPE,
    UNKNOWN_CARD_HANDLE,
)
from typing import Any


class RecallToolRegistry:
    @staticmethod
    def _check_search(parsed: SearchInput, args: dict[str, Any], state: AttemptState) -> list[dict]:
        """search_memories：只能搜热池；semantic 模式不适用 match。"""
        errors = []
        if parsed.scope_handle != "scope_hot":
            errors.append(INVALID_SCOPE.build("search_memories 只能使用 scope_hot"))
        if parsed.mode == "semantic" and "match" in args:
            errors.append(INAPPLICABLE_FIELD.build("semantic 模式省略 match"))
        return errors

    @staticmethod
    def _check_read(parsed: ReadInput, args: dict[str, Any], state: AttemptState) -> list[dict]:
        """read_memories：只能读本轮已展示过的卡片，不得自造句柄。"""
        return [UNKNOWN_CARD_HANDLE.build(card_handle=handle)
                for handle in parsed.card_handles if handle not in state.cards]

    @staticmethod
    def _check_list(parsed: ListInput, args: dict[str, Any], state: AttemptState) -> list[dict]:
        """list_memories：全范围入口、时间基准、模式字段与 fallback 依据。"""
        errors = []
        if parsed.scope_handle != "scope_all":
            errors.append(INVALID_SCOPE.build("list_memories 只能使用 scope_all"))
        if parsed.mode == "overview" and "history_clue" in args:
            errors.append(INAPPLICABLE_FIELD.build("overview 模式省略 history_clue"))
        if "match" in args and not parsed.keywords:
            errors.append(INAPPLICABLE_FIELD.build("无 keywords 时省略 match"))
        if parsed.mode == "fallback" and not any(
                e.get("name") == "search_memories" and e.get("executed")
                for e in state.trajectory):
            errors.append(FALLBACK_REQUIRES_SEARCH.build())
        return errors

    TOOL_SPECS: dict[str, ToolSpec] = {
        "search_memories": ToolSpec("search_memories", SearchInput, "search", "items", 2, _check_search.__func__),
        "read_memories": ToolSpec("read_memories", ReadInput, "read", "reads", 2, _check_read.__func__),
        "list_memories": ToolSpec("list_memories", ListInput, "list_memories", "items", 1, _check_list.__func__),
    }
