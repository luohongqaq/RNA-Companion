import asyncio
import json

import pytest


class MaterialCounter:
    def count_text(self, text):
        return len(text)


from module.memory_recall.agent.core.processing.selection.service import MemorySelectionService
from module.memory_recall.retrieval.core.processing.session import MemoryReadSession
from module.memory_recall.agent.core.domain.model import MemoryModelResult
from module.memory_recall.retrieval.core.domain.tools.search import SearchInput


def call(name, args):
    return MemoryModelResult({"role": "assistant", "tool_calls": [{"id": "call", "function": {
        "name": name, "arguments": json.dumps(args)}}]}, "tool_calls")


class Reader:
    def __init__(self, fail=False):
        self.fail = fail
        self.searches = []

    async def temporal_coverage(self):
        return {"metadata_scan_complete": True}

    async def search(self, query, ledger):
        self.searches.append(query)
        if self.fail:
            raise RuntimeError("private connection detail")
        card = ledger.register_candidate("doc")
        sections = {"memorable_moments": "用户拿到了offer，担心入职表现。"}
        handle = ledger.register_read("doc", sections, 1, "hash", kind="daily", title="求职")
        return {"status": "ok", "items": [{"card_handle": card, "read_handle": handle,
            "sections": sections, "title": "求职"}]}


def test_query_is_bounded_and_excludes_summaries_and_old_dialogue():
    history = [{"role": "user", "content": "不应带入的旧话题"},
               {"role": "context", "content": "摘要不是原话"},
               *[{"role": "assistant", "content": "近况" * 500} for _ in range(3)]]
    args = MemorySelectionService.initial_search("offer " * 300, history)
    parsed = SearchInput(**args)
    assert parsed.mode == "hybrid" and parsed.literal_terms == ["offer"]
    assert len(parsed.query) <= 600 and parsed.query.startswith("offer")
    assert "不应带入" not in parsed.query and "摘要不是原话" not in parsed.query
    assert MemorySelectionService.initial_search(" ", history) is None
    assert SearchInput(**MemorySelectionService.initial_search("工作让我有点担心", [])).mode == "semantic"
    from module.memory_recall.agent.core.processing.tools.validation import SCHEMAS, RecallToolInputValidator
    assert not RecallToolInputValidator.schema_errors(MemorySelectionService.initial_search("Java Python SQL RAG Linux Rust", []), SCHEMAS["search_memories"])


@pytest.mark.asyncio
async def test_first_model_can_finish_with_actual_prefetch_sections():
    reader = Reader()
    requests = []

    class Model:
        async def complete(self, request):
            requests.append(request)
            l0 = json.loads(request.messages[1]["content"])
            item = l0["PREFETCH"]["result"]["items"][0]
            assert l0["POLICY"]["remaining_calls_by_tool"]["search_memories"] == 1
            assert request.reasoning_effort == "high"
            return call("finish_selection", {"reason_code": "selected", "selected": [{
                "read_handle": item["read_handle"], "section_keys": ["memorable_moments"]}], "unresolved": []})

    result = await MemorySelectionService.select(user_input="offer", chat_context=[], instructions="原Skill", store=MemoryReadSession(reader),
        client=Model(), model="test", token_counter=MaterialCounter(), prefetch_enabled=True)
    assert len(requests) == 1 and len(reader.searches) == 1
    assert result["selected"][0]["document_id"] == "doc"
    assert result["trajectory"][0]["phase"] == "prefetch"


@pytest.mark.asyncio
async def test_failure_is_visible_and_model_can_search_again():
    reader = Reader(fail=True)
    requests = []

    class Model:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                l0 = json.loads(request.messages[1]["content"])
                assert l0["PREFETCH"]["result"]["status"] == "partial"
                assert "private connection" not in request.messages[1]["content"]
                reader.fail = False
                return call("search_memories", {"scope_handle": "scope_hot", "purpose": "补查", "mode": "semantic", "query": "入职"})
            items = json.loads(request.messages[-2]["content"])["items"]
            return call("finish_selection", {"reason_code": "selected", "selected": [{
                "read_handle": items[0]["read_handle"], "section_keys": ["memorable_moments"]}], "unresolved": []})

    result = await MemorySelectionService.select(user_input="offer", chat_context=[], instructions="原Skill", store=MemoryReadSession(reader),
        client=Model(), model="test", token_counter=MaterialCounter(), prefetch_enabled=True)
    assert result["reason_code"] == "selected" and len(reader.searches) == 2


@pytest.mark.asyncio
async def test_prefetch_cannot_select_unread_sections():
    class Model:
        async def complete(self, request):
            item = json.loads(request.messages[1]["content"])["PREFETCH"]["result"]["items"][0]
            return call("finish_selection", {"reason_code": "selected", "selected": [{
                "read_handle": item["read_handle"], "section_keys": ["entries"]}], "unresolved": []})

    result = await MemorySelectionService.select(user_input="offer", chat_context=[], instructions="原Skill", store=MemoryReadSession(Reader()),
        client=Model(), model="test", token_counter=MaterialCounter(), prefetch_enabled=True)
    assert result["reason_code"] == "incomplete" and result["selected"] == []


@pytest.mark.asyncio
async def test_empty_store_skips_prefetch_and_model_calls():
    class EmptyReader(Reader):
        async def has_documents(self):
            return False

    class Model:
        async def complete(self, request):
            raise AssertionError("空库不应触发选择模型调用")

    result = await MemorySelectionService.select(user_input="你好", chat_context=[], instructions="Skill",
        store=MemoryReadSession(EmptyReader()), client=Model(), model="test",
        token_counter=MaterialCounter(), prefetch_enabled=True)
    assert result["reason_code"] == "empty_store" and result["selected"] == []


@pytest.mark.asyncio
async def test_probe_failure_falls_back_to_normal_flow():
    class BrokenProbeReader(Reader):
        async def has_documents(self):
            raise RuntimeError("探测失败")

    requests = []

    class Model:
        async def complete(self, request):
            requests.append(request)
            item = json.loads(request.messages[1]["content"])["PREFETCH"]["result"]["items"][0]
            return call("finish_selection", {"reason_code": "selected", "selected": [{
                "read_handle": item["read_handle"], "section_keys": ["memorable_moments"]}], "unresolved": []})

    result = await MemorySelectionService.select(user_input="offer", chat_context=[], instructions="Skill",
        store=MemoryReadSession(BrokenProbeReader()), client=Model(), model="test",
        token_counter=MaterialCounter(), prefetch_enabled=True)
    assert result["reason_code"] == "selected" and len(requests) == 1


@pytest.mark.asyncio
async def test_prefetch_cancellation_propagates():
    class CancelReader(Reader):
        async def search(self, query, ledger):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await MemorySelectionService.select(user_input="offer", chat_context=[], instructions="原Skill", store=MemoryReadSession(CancelReader()),
            client=None, model="test", token_counter=MaterialCounter(), prefetch_enabled=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["read_memories", "list_memories"])
async def test_prefetch_allows_full_read_and_cold_fallback(tool):
    class FollowupReader(Reader):
        async def read(self, query, ledger):
            card = query.card_handles[0]
            sections = {"memorable_moments": "完整原因：用户担心没有相关经验。"}
            handle = ledger.register_read("doc", sections, 1, "hash", kind="daily", title="求职")
            return {"status": "ok", "reads": [{"card_handle": card, "read_handle": handle, "sections": sections}]}

        async def list_memories(self, query, ledger):
            assert query.mode == "fallback"
            return await self.search(None, ledger)

    requests = []

    class Model:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                item = json.loads(request.messages[1]["content"])["PREFETCH"]["result"]["items"][0]
                if tool == "read_memories":
                    args = {"purpose": "摘要未记载入职担心的原因，读取原文核实", "card_handles": [item["card_handle"]]}
                else:
                    args = {"scope_handle": "scope_all", "mode": "fallback", "purpose": "用户指向上次交流，热池候选未覆盖该次经历",
                            "history_clue": "上次聊过入职担心"}
                return call(tool, args)
            output = json.loads(request.messages[-2]["content"])
            assert output["status"] == "ok", output
            item = output["reads" if tool == "read_memories" else "items"][0]
            return call("finish_selection", {"reason_code": "selected", "selected": [{
                "read_handle": item["read_handle"], "section_keys": ["memorable_moments"]}], "unresolved": []})

    result = await MemorySelectionService.select(user_input="offer", chat_context=[], instructions="Skill", store=MemoryReadSession(FollowupReader()),
        client=Model(), model="test", token_counter=MaterialCounter(), prefetch_enabled=True)
    assert result["reason_code"] == "selected", result
