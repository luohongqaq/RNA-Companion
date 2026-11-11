"""一次召回选择的准备、工具循环和结束校验。"""
from __future__ import annotations
from module.shared.common.time import BackendTime
import asyncio
import copy
import json
import re
from collections.abc import Callable
from module.shared.character.document import CharacterSelectionView as CharacterCard
from datetime import datetime, timezone
from module.memory_recall.agent.core.domain.model import MemoryModelRequest, MemoryModelResult
from module.memory_recall.agent.core.domain.ports import MemoryModelPort
from module.memory_recall.agent.core.domain.state import AttemptState
from module.memory_recall.agent.core.domain.policy import Budget, DEADLINE_SECONDS, MAX_MODEL_CALLS
from module.memory_recall.agent.core.domain.selection_protocol import SelectionToolProtocol
from module.memory_recall.agent.core.processing.tools import SCHEMAS, ToolExecutor
from module.memory_recall.agent.core.processing.tools.registry import RecallToolRegistry
from module.memory_recall.agent.core.processing.tools.validation import RecallToolInputValidator
from module.memory_recall.agent.core.processing.tools.schemas import FINISH_SELECTION, SCOPE_ROOTS, TOOLS
from module.memory_recall.retrieval.core.processing.session import MemoryReadSession as MemoryStore
from module.memory_recall.shared.core.domain.budgeting import RecallMaterialBudget
from module.memory_recall.shared.core.domain.errors import (
    DUPLICATE_SECTION,
    FINAL_BUDGET_EXCEEDED,
    INCONSISTENT_SELECTION,
    INVALID_JSON,
    INVALID_TOOL_BATCH,
    MISSING_UNRESOLVED,
    SECTION_NOT_READ,
    UNKNOWN_READ_HANDLE,
)
from module.memory_recall.shared.core.domain.policy import SINGLE_DOC_TOKEN_LIMIT
from module.memory_recall.shared.core.domain.records import RecentRecord, VisibleItem
from time import perf_counter
from typing import Any


class MemorySelectionService:
    """一轮记忆选择的执行器：组装 L0、驱动循环、校验 finish 并交卷。

    输入：store 为 MemoryStore（或等价假件）；state 为本轮 AttemptState。
    输出：run() 返回最终结果；取消或输入过期抛 CancelledError。
    """

    def __init__(self, store: MemoryStore, state: AttemptState,
                 character_card: CharacterCard | None = None,
                 reasoning_effort: str = 'high', max_output_tokens: int = 2048,
                 thinking_enabled: bool = True, prefetch_enabled: bool = False, token_counter=None) -> None:
        self.store = store
        self.state = state
        self.token_counter = token_counter
        self.tools = ToolExecutor(store, state, token_counter)
        self.character_card = character_card
        self.memory_time_coverage = {"metadata_scan_complete": False}
        self.thinking_enabled = thinking_enabled
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.prefetch_enabled = prefetch_enabled
        self.prefetch_result = None

    @classmethod
    async def select(
            cls, *, user_input: str, chat_context: list[dict], instructions: str,
            store: MemoryStore, client: MemoryModelPort, model: str,
            max_model_calls: int = MAX_MODEL_CALLS, deadline_seconds: float = DEADLINE_SECONDS,
            budget: Budget | None = None, character_card: CharacterCard | None = None,
            max_output_tokens: int = 2048, is_current: Callable[[], bool] | None = None,
            prefetch_enabled: bool = False, token_counter=None,
    ) -> dict:
        if max_model_calls < 1 or deadline_seconds <= 0 or max_output_tokens < 1:
            raise ValueError("选择预算必须为正")
        state = AttemptState.init(deadline_seconds, max_model_calls, is_current, budget)
        attempt = cls(store, state, character_card=character_card,
                      reasoning_effort="high", thinking_enabled=True,
                      max_output_tokens=max_output_tokens, prefetch_enabled=prefetch_enabled,
                      token_counter=token_counter)
        return await attempt.run(user_input=user_input, chat_context=chat_context,
                                 instructions=instructions, client=client, model=model)

    # ---- 测试与执行入口的委托 ----

    def validate_args(self, tool_name: str, args: dict) -> list[dict]:
        """校验入口（委托 ToolExecutor）；返回错误列表，空列表表示通过。"""
        return self.tools.validate_args(tool_name, args)

    async def execute_tool(self, *, tool_name: str, args: dict) -> dict:
        """执行入口（委托 ToolExecutor）：执行一次读取工具并回填可见材料。"""
        return await self.tools.execute_tool(tool_name=tool_name, args=args)

    # ---- finish 校验 ----

    def validate_finish(self, args: dict) -> tuple[bool, dict | list]:
        """校验最终选择，不读取数据库也不重新编写记忆。

        功能：检查提交结构和结束状态一致性，要求 read_handle/section_keys
        来自本轮实际可见 read_set；检查分区去重及最终材料预算。
        输入：args 为 finish_selection 的 reason_code、selected、unresolved。
        输出：(accepted, payload)。通过时为 (True, 接受回执 dict)，回执包含
        status、finished 和提交字段；拒绝时为 (False, 错误列表)。不消耗工具次数，
        不修改状态，也不提取正文；计数和 materialize 由主循环负责。
        """
        state = self.state
        errors = RecallToolInputValidator.schema_errors(args, SCHEMAS["finish_selection"])
        if errors:
            return False, errors
        reason, selected = args["reason_code"], args["selected"]
        if reason == "selected" and not selected:
            errors.append(INCONSISTENT_SELECTION.build("selected 状态必须选择材料"))
        if reason in ("context_sufficient", "no_relevant_memory") and selected:
            errors.append(INCONSISTENT_SELECTION.build("该结束状态要求空选择"))
        if reason == "incomplete" and not args["unresolved"]:
            errors.append(MISSING_UNRESOLVED.build())
        seen, documents, materials = set(), set(), []
        for item in selected:
            handle = item["read_handle"]
            entry = state.read_set.get(handle)
            if entry is None:
                errors.append(UNKNOWN_READ_HANDLE.build(read_handle=handle))
                continue
            document_id = entry["document_id"]
            documents.add(document_id)
            sections = {}
            for key in item["section_keys"]:
                if key not in entry["sections"]:
                    errors.append(SECTION_NOT_READ.build(read_handle=handle, section_key=key))
                elif (document_id, key) in seen:
                    errors.append(DUPLICATE_SECTION.build(section_key=key))
                else:
                    seen.add((document_id, key))
                    sections[key] = entry["sections"][key]
            materials.append(sections)
        if len(documents) > state.budget.final_doc_limit or RecallMaterialBudget.count_tokens(materials, self.token_counter) > state.budget.final_token_limit:
            errors.append(FINAL_BUDGET_EXCEEDED.build())
        if errors:
            return False, errors
        return True, {"status": "accepted", "finished": True, **copy.deepcopy(args), "errors": []}

    # ---- L0 组装 ----

    def _policy(self) -> dict[str, Any]:
        """生成供模型查看的预算信息：剩余额度快照，不含 read_set 或后台轨迹。"""
        state = self.state
        return {
            "remaining_model_calls": max(0, state.max_model_calls - state.model_calls),
            "remaining_tool_calls": max(0, state.budget.max_tool_calls - state.tool_calls),
            "remaining_calls_by_tool": {name: max(0, spec.max_calls - state.tool_calls_by_name.get(name, 0))
                                        for name, spec in RecallToolRegistry.TOOL_SPECS.items()},
            "remaining_output_tokens": max(0, state.budget.output_token_limit - state.output_tokens),
            "max_selected_documents": state.budget.final_doc_limit,
            "max_selected_tokens": state.budget.final_token_limit,
            "single_document_token_limit": SINGLE_DOC_TOKEN_LIMIT,
            "remaining_seconds": state.target_remaining(),
            "time_guidance": "剩余秒数是完成目标，不是取消请求的时限。接近或超过目标时尽快完整提交已有证据，不要因目标归零将已完成工作报为超时。",
        }

    def _recent_item(self, record: RecentRecord) -> VisibleItem | None:
        """核验并展示本轮签发句柄对应的全部缓存正文，不扩张或隐藏分区。"""
        card, handle = record.get("card_handle"), record.get("read_handle")
        if card is None or handle is None:
            return None  # 缺句柄的账本记录不可信，不进 L0。
        # 检查条目是否在读取集，避免模型选择未读取的条目。
        entry = self.store.read_set.get(handle)
        if entry is None or self.store.cards.get(card) != record.get("document_id"):
            return None
        if (entry["revision"], entry["content_hash"]) != (record.get("revision"), record.get("content_hash")):
            return None
        sections = copy.deepcopy(record.get('sections', {}))
        if not sections or sections != entry['sections']:
            return None
        item = VisibleItem(card_handle=card, read_handle=handle,
                           title=record["title"], sections=sections)
        if entry.get('time_context'):
            item['time_context'] = copy.deepcopy(entry['time_context'])
            # Re-signed summary handles retain the exact same temporal metadata.
            self.store.read_set[handle]['time_context'] = copy.deepcopy(entry['time_context'])
        return item

    def build_l0(self, *, current_input: str, chat_context: list[dict] | None, now: datetime) -> dict:
        """RECENT_RETRIEVALS含近期更新(5张)与最近已读(10张)两组正文卡片。"""
        updated, recent = [], []
        for record in self.store.updated_records:
            item = self._recent_item(record)
            if item is not None and RecallMaterialBudget.count_tokens(updated + [item], self.token_counter) <= self.state.budget.final_token_limit:
                self.tools.register_visible(item)
                updated.append(item)
        for record in reversed(self.store.recent_retrievals()):
            if self.prefetch_result and record.get("read_handle") in {
                    item.get("read_handle") for item in self.prefetch_result.get("items", [])}:
                continue
            item = self._recent_item(record)
            if item is None or RecallMaterialBudget.count_tokens(updated + recent + [item], self.token_counter) > self.state.budget.final_token_limit:
                continue
            self.tools.register_visible(item)
            recent.append(item)
        recent.reverse()
        local_now = BackendTime.local(now)
        l0 = {
            "CURRENT_INPUT": current_input,
            "CHAT_CONTEXT": copy.deepcopy(chat_context or []),
            "RECENT_RETRIEVALS": {"recent_update_memory": updated, "recent_retrievals": recent},
            "ROOTS": copy.deepcopy(SCOPE_ROOTS),
            "NOW": BackendTime.utc(now).isoformat(),
            "BACKEND_LOCAL_DATE": local_now.date().isoformat(),
            "BACKEND_UTC_OFFSET": local_now.strftime("%z"),
            "MEMORY_TIME_COVERAGE": copy.deepcopy(self.memory_time_coverage),
            "POLICY": self._policy(),
        }
        if self.character_card is not None:
            l0["CHARACTER_CONTEXT"] = self.character_card.selection_context()
        if self.prefetch_result is not None:
            l0["PREFETCH"] = {
                "result": copy.deepcopy(self.prefetch_result),
                "source": "程序按当前输入与近期真实对话执行的热池预检索；计入运行时预算。",
            }
        return l0

    # ---- 主循环 ----

    def _unfinished(self, message: str) -> dict:
        """兜底结果：未完成即空选择，绝不自动采用预读内容。"""
        return {"reason_code": "incomplete", "selected": [], "unresolved": [message],
                "trajectory": self.state.trajectory}

    async def _request_model(self, client: MemoryModelPort, model: str,
                             messages: list[Any], tools: list[dict[str, Any]],
                             finish_only: bool) -> tuple[MemoryModelResult | None, str | None]:
        """请求一次模型（双层超时：SDK 请求超时 + wait_for 守住整轮 deadline）。

        返回 (response, None) 或 (None, 失败原因)；CancelledError 原样上抛。
        """
        request_messages = copy.deepcopy(messages)
        # 告知模型当前运行时预算
        request_messages.append({
            'role': 'user',
            'content': json.dumps({
                'kind': 'runtime_budget_update',
                'POLICY': self._policy(),
                'current_model_call': self.state.model_calls,
                'submission_only': finish_only,
                'note': '这是本次模型请求开始前的运行时预算，以此替代初始POLICY及历史思考中的剩余估计。'
            }, ensure_ascii=False),
        })
        if finish_only:
            # DeepSeek thinking 模式不支持指定函数的 tool_choice。
            # 只提供 finish 工具并明确要求提交；服务端仍拒绝其他工具调用。
            request_messages.append({
                "role": "user",
                "content": "本轮已进入提交阶段，只能调用 finish_selection。请提交已有的已核验材料；"
                           "按工具定义选择结束状态，正常查找结束但无可用材料不等于未完成。"
                           "仅必要核验未完成或执行失败影响目标时，在 unresolved 说明缺口并使用 incomplete。"
                           "不要继续检索或直接回复正文。",
            })
        try:
            response = await asyncio.wait_for(client.complete(MemoryModelRequest(
                model=model, messages=request_messages, tools=tools,
                thinking_enabled=self.thinking_enabled,
                reasoning_effort=self.reasoning_effort,
                max_output_tokens=self.max_output_tokens,
                timeout_seconds=self.state.remaining())), timeout=self.state.remaining())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return None, ("模型请求超时" if isinstance(exc, TimeoutError)
                          else f"模型请求未完成（{type(exc).__name__}）")
        return response, None

    async def _dispatch_call(self, name: str, args: dict, *, mixed_batch: bool,
                             finish_only: bool) -> tuple[dict, dict | None]:
        """三分支分发一次工具调用，返回 (回填结果, 最终结果或 None)。

        分支一（协议/预算违规）→ invalid_tool_batch；分支二（finish_selection）
        → 校验并可能当场交卷，第二个返回值非 None 时主循环立即结束；
        分支三（读取工具）→ execute_tool。
        """
        state = self.state
        if mixed_batch or (finish_only and name != "finish_selection") or state.tool_calls >= state.budget.max_tool_calls:
            if state.tool_calls < state.budget.max_tool_calls:
                state.tool_calls += 1
            return self.tools.record_result(name, args, {"status": "partial", "errors": [
                INVALID_TOOL_BATCH.build()]}), None
        if name != "finish_selection":
            return await self.tools.execute_tool(tool_name=name, args=args), None
        state.tool_calls += 1
        accepted, payload = self.validate_finish(args)
        result = payload if accepted else {"status": "rejected", "finished": False, "errors": payload}
        self.tools.record_result(name, args, result)
        if not accepted:
            state.finish_rejections += 1
            return result, None
        state.ensure_current()
        try:
            material = self.store.materialize(args["selected"])
        except (ValueError, KeyError):
            # validate_finish 与 materialize 查同一本 read_set；此处失败说明校验
            # 有洞，宁可 incomplete 也不把未核验内容当结果。
            return result, self._unfinished("最终材料提取失败")
        return result, {"reason_code": args["reason_code"], "selected": material,
                        "unresolved": args["unresolved"], "trajectory": state.trajectory}

    async def run(self, *, user_input: str, chat_context: list[dict],
                  instructions: str, client: MemoryModelPort, model: str) -> dict:
        """驱动选择循环直到交卷或预算耗尽；交卷成功时立即返回最终结果。"""
        state = self.state
        state.ensure_current()
        # 空库探测：记忆库无任何正式文档时，预检索/选择/总结都不会有产出，
        # 直接交空选择省掉一次选择模型调用；探测失败不阻塞主流程。
        has_documents = getattr(self.store, 'has_documents', None)
        if has_documents is not None:
            try:
                if not await asyncio.wait_for(has_documents(), timeout=.5):
                    return {"reason_code": "empty_store", "selected": [], "unresolved": [],
                            "trajectory": state.trajectory}
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        provider = getattr(self.store, 'temporal_coverage', None)
        if provider is not None:
            try:
                self.memory_time_coverage = await asyncio.wait_for(provider(), timeout=min(.5, state.remaining()))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.memory_time_coverage = {"metadata_scan_complete": False}
        if self.prefetch_enabled:
            args = self.initial_search(user_input, chat_context)
            if args is not None:
                started = perf_counter()
                self.prefetch_result = await self.execute_tool(tool_name="search_memories", args=args)
                state.trajectory[-1].update(phase="prefetch", seconds=perf_counter() - started)
        l0 = self.build_l0(current_input=user_input, chat_context=chat_context,
                           now=datetime.now(timezone.utc))
        messages: list[Any] = [{"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(l0, ensure_ascii=False)}]
        for index in range(state.max_model_calls):
            # 每轮先查取消与预算：耗尽即收尾，不再花模型调用。
            state.ensure_current()
            if state.remaining() == 0 or state.tool_calls >= state.budget.max_tool_calls:
                return self._unfinished("时间或工具调用预算已耗尽")
            # 收尾时只提供 finish_selection，并用兼容 thinking 模式的 auto 请求提交。
            finish_only = state.must_force_finish(index)
            tools = [FINISH_SELECTION] if finish_only else TOOLS
            state.model_calls += 1
            response, failure_reason = await self._request_model(
                client, model, messages, tools, finish_only)
            if failure_reason is not None:
                return self._unfinished(failure_reason)
            # 运行工具函数前先检查是否还有剩余时间，并把模型的返回结果记录添加到消息列表中
            state.ensure_current()
            if state.remaining() == 0:
                return self._unfinished("模型返回时已超过截止时间")
            try:
                _, calls = SelectionToolProtocol.extract_message(response)
            except (AttributeError, IndexError, TypeError, ValueError):
                return self._unfinished("模型返回的消息结构无效")
            # 按官方示例保留完整消息，包括 content、reasoning_content、tool_calls。
            messages.append(response.message)
            if not calls:
                # 模型只说话不调工具：提醒一次并消耗一次模型额度，倒逼其收敛。
                messages.append({"role": "user",
                                 "content": "请调用提供的工具；材料足够或无需记忆时，用 finish_selection 提交。"})
                continue
            if not SelectionToolProtocol.call_ids_valid(calls):
                return self._unfinished("模型返回的工具调用 ID 无效")
            mixed_batch = SelectionToolProtocol.has_mixed_finish(calls)
            # 逐个调用分发；每个 tool_call 必有恰好一条配对的 tool 结果消息。
            for call in calls:
                name = call["function"].get("name", "")
                try:
                    args = SelectionToolProtocol.parse_arguments(call)
                except (ValueError, TypeError):
                    state.tool_calls += 1
                    result = self.tools.record_result(name, {}, {"status": "partial", "errors": [
                        INVALID_JSON.build()]})
                else:
                    result, final_result = await self._dispatch_call(
                        name, args, mixed_batch=mixed_batch, finish_only=finish_only)
                    if final_result is not None:
                        return final_result
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(result, ensure_ascii=False)})
            if state.finish_rejections >= 2:
                return self._unfinished("最终选择校验失败，修正次数已耗尽")
        return self._unfinished("模型调用预算耗尽，未形成有效最终选择")

    @staticmethod
    def initial_search(user_input: str, chat_context: list[dict]) -> dict | None:
        current = user_input.strip()
        if not current:
            return None
        recent = [f"{item['role']}: {item['content']}" for item in chat_context
                  if item.get('role') in ('user', 'assistant') and item.get('content')][-3:]
        # 当前输入优先保留；近期对话仅补指代，检索文本有界，不改模型看到的真实上下文。
        query = current[:400]
        if recent:
            query += "\n近期对话：\n" + "\n".join(recent)[-180:]
        # 只采用可直接从原话提取的英文专名/编号和引号短语，不凭程序猜中文关键词。
        terms = []
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9_+#.-]{1,39}|[“「『\"]([^”」』\"\n]{2,20})[”」』\"]", current):
            term = match.group(1) or match.group(0)
            if term not in terms:
                terms.append(term)
        result = {"scope_handle": "scope_hot",
                  "purpose": "提前查找有助于理解当前对话、自然接话的正式历史事实与背景，供模型按本轮记忆目标筛选",
                  "mode": "hybrid" if terms else "semantic", "query": query, "limit": 3}
        if terms:
            result.update(literal_terms=terms[:4], match="any")
        return result