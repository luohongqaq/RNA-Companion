"""工具执行器：单次读取工具调用的完整流水线。

职责：参数三层校验（结构/语义/准入）→ 预算与去重闸门 → 调 store →
返回体裁剪 → 可见性登记 → 近期账本合并。只管"一次工具调用怎么安全执行"；
整轮循环在 agent.selection，工具档案在 registry.py，状态与预算在 state.py。
"""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
from module.memory_recall.agent.core.domain.state import AttemptState
from module.memory_recall.agent.core.domain.tool import ToolSpec
from module.memory_recall.agent.core.processing.tools.registry import RecallToolRegistry
from module.memory_recall.agent.core.processing.tools.validation import SCHEMAS, RecallToolInputValidator
from module.memory_recall.retrieval.core.domain.tools.listing import ListInput, ListResult
from module.memory_recall.retrieval.core.domain.tools.read import ReadInput, ReadResult
from module.memory_recall.retrieval.core.domain.tools.search import SearchInput, SearchResult
from module.memory_recall.retrieval.core.processing.session import MemoryReadSession as MemoryStore
from module.memory_recall.shared.core.domain.budgeting import RecallMaterialBudget
from module.memory_recall.shared.core.domain.errors import (
    DEADLINE_EXCEEDED,
    DUPLICATE_REQUEST,
    INVALID_ARGUMENTS,
    INVALID_MATERIAL,
    RESPONSE_BUDGET_REACHED,
    TOOL_BUDGET_EXHAUSTED,
    TOOL_EXECUTION_FAILED,
    UNKNOWN_TOOL,
)
from module.memory_recall.shared.core.domain.policy import RECENT_LIMIT
from module.memory_recall.shared.core.domain.records import RecentRecord, VisibleItem
from pydantic import ValidationError
from typing import Any


class ToolExecutor:
    """单个读取工具的执行器：持有 store 与本轮 state，方法为流水线各环节。"""

    @staticmethod
    def _merge_recent(recent_before: dict[str, RecentRecord], recent_after: dict[str, RecentRecord],
                     accepted_docs: set[str]) -> dict[str, RecentRecord]:
        """近期账本合并：保留快照 + 本轮实际接受条目的最新记录，截断到上限。"""
        merged = dict(recent_before)
        for document_id in recent_after:
            if document_id not in accepted_docs:
                continue
            merged.pop(document_id, None)
            merged[document_id] = recent_after[document_id]
        return dict(list(merged.items())[-RECENT_LIMIT:])

    def __init__(self, store: MemoryStore, state: AttemptState, token_counter) -> None:
        self.store = store
        self.state = state
        self.token_counter = token_counter

    # ---- 参数校验（语义层 + 准入层） ----

    def _validate(self, tool_name: str, args: dict) -> tuple[list[dict], SearchInput | ReadInput | ListInput | None]:
        """三层校验：Schema 结构 → 输入模型语义 → ToolSpec 准入检查。

        返回 (错误列表, 解析后的输入模型或 None)；解析出的默认值不写回 args。
        """
        spec = RecallToolRegistry.TOOL_SPECS.get(tool_name)
        if spec is None:
            return [UNKNOWN_TOOL.build(f"未知读取工具 {tool_name}")], None
        # 检查 Schema错误, 正确返回空列表，有错返回语法错误列表
        errors = RecallToolInputValidator.schema_errors(args, SCHEMAS[tool_name])
        if errors:
            return errors, None
        try:
            tool_input = spec.input_model.model_validate(args)
        except ValidationError as exc:  # 输入模型的静态校验失败（含跨字段规则）统一在此汇总
            return [INVALID_ARGUMENTS.build(f"{'.'.join(map(str, e['loc']))}: {e['msg']}")
                    for e in exc.errors()], None
        return spec.admission_checks(tool_input, args, self.state), tool_input

    def validate_args(self, tool_name: str, args: dict) -> list[dict]:
        """校验三个读取工具的参数与本轮运行条件；返回错误列表，空列表表示通过。"""
        return self._validate(tool_name, args)[0]

    # ---- 可见性登记（本轮模型实际看到什么） ----

    def register_visible(self, item: VisibleItem) -> None:
        """登记本轮实际展示的卡片和正文，并固定已见版本。

        只将实际返回的分区复制到 state.read_set。
        输入：item 为一个工具结果或 L0 摘要条目。
        输出：成功返回 None；句柄、正文或版本不一致时抛 ValueError
        （内部不变量，不跨模型边界）。只有标题的卡片只登记 cards。
        """
        state = self.state
        card = item.get("card_handle")
        document_id = self.store.cards.get(card)
        if document_id is None:
            raise ValueError("store 返回了未登记的 card_handle")
        handle = item.get("read_handle")
        sections = item.get("sections", {})
        if handle is not None:
            entry = self.store.read_set.get(handle)
            if entry is None or entry["document_id"] != document_id or not sections:
                raise ValueError("正文句柄与文档不一致或没有实际正文")
            if any(k not in entry["sections"] or entry["sections"][k] != v for k, v in sections.items()):
                raise ValueError("返回正文与已登记正文不一致")
            registered_entry = copy.deepcopy(entry)
            registered_entry["sections"] = copy.deepcopy(sections)
            state.read_set[handle] = registered_entry
        state.cards[card] = document_id

    def _register_visible_items(self, result: dict[str, Any],
                                materials_key: str) -> tuple[dict[str, Any], set[str]]:
        """逐条核对并登记结果里的材料；返回 (结果, 实际接受条目对应的文档集合)。"""
        shown_items, accepted_docs = [], set()
        for item in result.get(materials_key, []):
            try:
                self.register_visible(item)
            except ValueError as exc:
                result["status"] = "partial"
                result.setdefault("errors", []).append(
                    INVALID_MATERIAL.build(str(exc), card_handle=item.get("card_handle")))
            else:
                shown_items.append(item)
                if item.get("read_handle"):
                    accepted_docs.add(self.state.cards[item["card_handle"]])
        if materials_key in result:
            result[materials_key] = shown_items
        return result, accepted_docs

    # ---- 结果登记与预算裁剪 ----

    def record_result(self, name: str, args: dict[str, Any], result: dict[str, Any], *,
                      executed: bool = False, fingerprint: str | None = None) -> dict[str, Any]:
        """登记一次工具结果及其用量：累计返回 token 数并保存轨迹条目。"""
        count = RecallMaterialBudget.count_tokens(result, self.token_counter)
        self.state.output_tokens += count
        self.state.trajectory.append({"name": name, "arguments": copy.deepcopy(args),
                                      "status": result.get("status"), "errors": result.get("errors", []),
                                      "executed": executed, "fingerprint": fingerprint,
                                      "output_tokens": count})
        return result

    def _fit_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """使工具返回体适合剩余材料预算。

        功能：预留错误反馈空间，从尾部移除完整 items/reads 条目并标记 partial；
        不截断正文，不生成代表不完整全文的句柄。read 的逐项错误保留 card_handle。
        输入：result 为已序列化工具结果 dict。
        输出：裁剪后的结果；最小返回体仍超限时返回精简错误。可能原地修改 result，
        但不登记已读材料，后续只登记真正留下的条目。
        """
        output_budget = max(0, self.state.budget.output_token_limit
                            - self.state.output_tokens - self.state.budget.error_reserve)
        materials_key = "reads" if "reads" in result else "items"
        over_budget_items = []
        while RecallMaterialBudget.count_tokens(result, self.token_counter) > output_budget and result.get(materials_key):
            over_budget_items.append(result[materials_key].pop())
        if over_budget_items:
            result["status"] = "partial"
            if "coverage" in result:
                result["coverage"]["response_budget_reached"] = True
            if materials_key == "reads":
                result.setdefault("errors", []).extend(
                    RESPONSE_BUDGET_REACHED.build("该完整文档因预算未返回", card_handle=i["card_handle"])
                    for i in over_budget_items)
            else:
                result.setdefault("errors", []).append(
                    RESPONSE_BUDGET_REACHED.build("部分完整条目因预算未返回"))
            while RecallMaterialBudget.count_tokens(result, self.token_counter) > output_budget and result.get(materials_key):
                item = result[materials_key].pop()
                if materials_key == "reads":
                    result["errors"].append(RESPONSE_BUDGET_REACHED.build(
                        "该完整文档因预算未返回", card_handle=item["card_handle"]))
        if RecallMaterialBudget.count_tokens(result, self.token_counter) > output_budget:
            return {"status": "partial",
                    "errors": [RESPONSE_BUDGET_REACHED.build("返回预算不足，请结束选择")]}
        return result

    # ---- 执行流水线 ----

    async def _call_store(self, spec: ToolSpec,
                          tool_input: SearchInput | ReadInput | ListInput) -> SearchResult | ReadResult | ListResult:
        """按 ToolSpec 分发到 store 方法；设了截止时以剩余时间为超时。"""
        pending = getattr(self.store, spec.store_method)(tool_input)
        remaining = self.state.remaining()
        if remaining is None:
            return await pending
        return await asyncio.wait_for(pending, remaining)

    async def execute_tool(self, *, tool_name: str, args: dict) -> dict:
        """执行一次读取工具，并回填可见材料与预算状态。

        功能：检查次数、时间、参数和成功请求去重，按 registry 的 ToolSpec
        分发到 store 方法；序列化并限制返回量，登记本轮实际可见内容。
        store 已负责登记近期摘要，本函数仅回滚被丢弃或失败条目的近期记录，
        不重复调用 record_retrievals。finish_selection 不经本函数执行。
        输入：tool_name 为读取工具名；args 为已解析参数对象，本函数仍执行校验。
        输出：可作为对应 tool_call_id 结果的 dict，包含材料或 partial+errors；
        同时更新计数、轨迹和可见账本。取消或输入过期抛 CancelledError，其他
        执行异常转换为不泄露连接配置的错误结果。
        """
        state = self.state
        state.ensure_current()
        if state.tool_calls >= state.budget.max_tool_calls - 1:
            # 一次模型响应可以携带一串工具调用（calls 是列表，模型可以一口气发几个工具调用）。
            # 主循环的检查只发生在请求发出之前，它检查完 tool_calls 就去等模型了；等响应回来，批内逐个执行时计数从低位一路 +1，
            # 因此这里需要留下一次工具调用用于 finish_selection
            return self.record_result(tool_name, args, {"status": "partial", "errors": [
                TOOL_BUDGET_EXHAUSTED.build("保留最后一次工具调用用于 finish_selection")]})
        state.tool_calls += 1
        state.tool_calls_by_name[tool_name] = state.tool_calls_by_name.get(tool_name, 0) + 1
        # 拿到对应的工具说明书
        spec = RecallToolRegistry.TOOL_SPECS.get(tool_name)
        # 校验输入参数、剩余时间额度、剩余调用次数，将所有的error检查完一同返回给上游
        errors, tool_input = self._validate(tool_name, args)
        if spec is not None and state.tool_calls_by_name[tool_name] > spec.max_calls:
            errors.append(TOOL_BUDGET_EXHAUSTED.build(f"{tool_name} 调用次数已耗尽"))
        if state.remaining() == 0:
            errors.append(DEADLINE_EXCEEDED.build())
        if state.output_tokens >= state.budget.output_token_limit - state.budget.error_reserve:
            errors.append(RESPONSE_BUDGET_REACHED.build())
        if errors:
            return self.record_result(tool_name, args, {"status": "partial", "errors": errors})
        # 防止模型在同一个会话里反复调用完全相同的工具请求
        normalized_args = tool_input.model_dump(mode="json", exclude_none=True)
        normalized_args.pop("purpose", None)
        fingerprint = hashlib.sha256(
            json.dumps([tool_name, normalized_args], sort_keys=True).encode()).hexdigest()
        if any(e.get("fingerprint") == fingerprint and e.get("status") == "ok" for e in state.trajectory):
            return self.record_result(tool_name, args, {"status": "partial", "errors": [
                DUPLICATE_REQUEST.build()]})
        # 拿到调用工具前的近期记录
        recent_before = copy.deepcopy(self.store.recent_records)
        try:
            store_result = await self._call_store(spec, tool_input)
            state.ensure_current()
            if state.remaining() == 0:
                raise TimeoutError("工具返回时已到截止时间")
            result = (store_result.model_dump(mode="json") if hasattr(store_result, "model_dump")
                      else copy.deepcopy(store_result))
            result = self._fit_output(result)
            result, accepted_docs = self._register_visible_items(result, spec.result_key)
            # store 先登记后返回；被预算丢弃的条目不得被当成模型已读。
            self.store.recent_records = self._merge_recent(
                recent_before, self.store.recent_records, accepted_docs)
            return self.record_result(tool_name, args, result, executed=True, fingerprint=fingerprint)
        except asyncio.CancelledError:
            self.store.recent_records = recent_before
            raise
        except Exception as exc:
            self.store.recent_records = recent_before
            error_spec = DEADLINE_EXCEEDED if isinstance(exc, TimeoutError) else TOOL_EXECUTION_FAILED
            # 不回显异常全文，避免连接配置或凭据进入模型消息。
            return self.record_result(tool_name, args, {"status": "partial", "errors": [
                error_spec.build(f"工具未完成（{type(exc).__name__}）")]},
                executed=True, fingerprint=fingerprint)
