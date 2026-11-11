"""正式记忆读取业务：分区检索、文档聚合、固定快照和句柄登记。"""
from __future__ import annotations
import copy
import json
from module.memory_recall.retrieval.core.domain.ports import (
    MemoryDocumentRepository,
    MemorySearchRepository,
    ReadLedger,
)
from module.memory_recall.retrieval.core.domain.tools.common import (
    Status,
    TimeContext,
    TimeRange,
    ToolError,
    TopicTimeContext,
)
from module.memory_recall.retrieval.core.domain.tools.listing import ListCoverage, ListInput, ListItem, ListResult
from module.memory_recall.retrieval.core.domain.tools.read import ReadEntry, ReadError, ReadInput, ReadResult
from module.memory_recall.retrieval.core.domain.tools.search import (
    SearchCoverage,
    SearchInput,
    SearchItem,
    SearchResult,
)
from module.memory_recall.retrieval.core.domain.document_snapshot import RecallDocumentSnapshot
from module.memory_recall.shared.core.domain.policy import SINGLE_DOC_TOKEN_LIMIT, SUMMARY_CARDS_PER_KIND
from module.memory_recall.shared.core.domain.records import ReadRecord, RecentRecord
from module.memory_recall.shared.core.domain.budgeting import RecallMaterialBudget
from typing import Any


class MemoryReadService:
    @staticmethod
    def _hit_count(text: str, terms: list[str]) -> int:
        """大小写不敏感地统计命中的词数（去重词的覆盖数，不是出现次数）。
        只用于 any/all 的入选判定，不参与排序。"""
        if not terms:
            return 0
        text_lower = text.lower()
        return sum(1 for term in terms if term.lower() in text_lower)

    @staticmethod
    def _occurrence_count(text: str, terms: list[str]) -> int:
        """大小写不敏感地统计词表的总命中次数（出现几次算几次）。
        any 模式的相关性排序依据：命中次数多说明确实在聊这个话题。"""
        if not terms:
            return 0
        text_lower = text.lower()
        return sum(text_lower.count(term.lower()) for term in terms)

    @staticmethod
    def _match_hit(text: str, terms: list[str], match: str) -> bool:
        """any=任一词命中（覆盖数≥1）；all=全部词在同一文本内命中。一次搜索只能出现any或all

        大小写不敏感是刻意选择——用户常混用 python/Python、rag/RAG；
        子串而非词元，保住"培训"命中"培训班"这类词内包含。"""
        count = MemoryReadService._hit_count(text, terms)
        if match == "all":
            return bool(terms) and count == len(terms)
        return count > 0

    @staticmethod
    def _time_key(payload: dict) -> float:
        """按记忆日期或得知日期排序；没有日期时排在最后。"""
        dates = payload.get("record_dates", [])
        return float(max(dates).replace("-", "")) if dates else 0.0

    def __init__(self, documents: MemoryDocumentRepository, search: MemorySearchRepository,
                 character_id: str, token_counter) -> None:
        self._documents, self._search = documents, search
        self.character_id = character_id
        self._token_counter = token_counter
        self._snapshot: RecallDocumentSnapshot | None = None
        self._pinned_documents: dict[str, dict] = {}

    def _in_scope(self, payload: dict) -> bool:
        return payload.get("character_id") == self.character_id

    def _pin_payload(self, payload: dict) -> dict:
        if not self._in_scope(payload):
            raise ValueError("记忆不属于本次角色")
        return self._pinned_documents.setdefault(payload["document_id"], copy.deepcopy(payload))

    async def _load_payload(self, document_id: str) -> dict | None:
        return next((p for p in (await self._document_snapshot()).payloads if p["document_id"] == document_id), None)

    async def load_records(self, document_ids: list[str]) -> dict[str, ReadRecord]:
        wanted = set(document_ids)
        result = {}
        for payload in (await self._document_snapshot()).payloads:
            if payload["document_id"] not in wanted or self._tokens(payload["sections"]) > SINGLE_DOC_TOKEN_LIMIT:
                continue
            result[payload["document_id"]] = ReadRecord(document_id=payload["document_id"],
                kind=payload["kind"], title=payload["title"], sections=copy.deepcopy(payload["sections"]),
                revision=payload["revision"], content_hash=payload["content_hash"],
                conversation_time=payload["conversation_time"],
                time_context=self._time_context(payload).model_dump(mode="json"))
        return result

    async def has_documents(self) -> bool:
        """快照内是否存在任何正式文档；空库时调用方可直接跳过记忆选择。"""
        return bool((await self._document_snapshot()).payloads)

    async def temporal_coverage(self) -> dict:
        """本轮固定快照的时间上界，含冷热池；不把它当作真实生活或来源入库水位。"""
        snapshot = await self._document_snapshot()
        return snapshot.coverage()

    async def _time_candidates(self, pool, kinds, window, unknown_policy):
        snapshot = await self._document_snapshot()
        selected, info = snapshot.filter_by_time(window, unknown_policy, pool=pool, kinds=kinds)
        info.update(includes_hot_and_cold=pool is None)
        return selected, info


    async def search(self, query: SearchInput, session: ReadLedger) -> SearchResult:
        """热池grep/semantic/hybrid检索；命中正式版本后签发句柄，按类型限制卡片数量。"""
        time_candidates, time_info = await self._time_candidates(
            'hot', query.kinds, query.time_range, query.unknown_event_time)
        errors: list[ToolError] = []

        grep_hits: list[dict] = []
        literal_done = None
        if query.mode in ("grep", "hybrid"):
            # 字面匹配：搜索payload。入选只看覆盖数（_hit_count 判 any/all）；
            # any 的排序用总命中次数（出现多说明确实在聊），all 约束已足够强、
            # 不排相关性，score 记 0 直接落时间序
            try:
                scanned = time_candidates
                for payload in scanned:
                    count = MemoryReadService._hit_count(payload["match_text"], query.literal_terms)
                    passed = (count == len(query.literal_terms)
                              if query.match == "all" else count > 0)
                    if passed:
                        score = (0 if query.match == "all"
                                 else MemoryReadService._occurrence_count(payload["match_text"], query.literal_terms))
                        grep_hits.append({"payload": payload, "score": score})
                literal_done = True
            except Exception as exc:
                literal_done = False
                errors.append(ToolError(code="grep_channel_failed", message=f"grep 通道失败：{type(exc).__name__}"))

        semantic_payloads: list[dict] = []
        semantic_done = None
        if query.mode in ("semantic", "hybrid"):
            try:
                semantic_payloads = await self._semantic_recall(
                    query.query, time_candidates, query.limit * len(query.kinds))
                semantic_done = True
            except Exception as exc:
                semantic_done = False
                errors.append(ToolError(code="semantic_channel_failed", message=f"semantic 通道失败：{type(exc).__name__}"))

        # 合并去重：hybrid 下同一篇双通道命中则 matched_by 记两路；
        # rrf_rank 保留 semantic 召回名次，score 保留 grep 相关性（any=命中次数）
        merged: dict[str, dict] = {}
        for hit in grep_hits:
            merged.setdefault(hit["payload"]["document_id"],
                              {"payload": hit["payload"], "matched_by": [],
                               "rrf_rank": None, "score": hit["score"]})["matched_by"].append("grep")
        for rank, payload in enumerate(semantic_payloads):
            entry = merged.setdefault(payload["document_id"],
                                      {"payload": payload, "matched_by": [],
                                       "rrf_rank": None, "score": 0})
            entry["matched_by"].append("semantic")
            entry["rrf_rank"] = rank

        def _sort_key(c: dict) -> tuple[int, int, tuple[int, int], float, str]:
            # 双通道命中优先 → 命中次数降序（仅 any；all 不排相关性）
            # → semantic 召回名次 → 最近来源对话时间 → document_id
            rank = c["rrf_rank"]
            return (-len(c["matched_by"]),
                    -c["score"],
                    (0, rank) if rank is not None else (1, 0),
                    -MemoryReadService._time_key(c["payload"]),
                    c["payload"]["document_id"])

        candidates = sorted(merged.values(), key=_sort_key)
        # 按 kind 配额截断：每类最多 limit 张，防止一类挤掉另一类
        by_kind: dict[str, list[dict]] = {}
        for candidate in candidates:
            by_kind.setdefault(candidate["payload"]["kind"], []).append(candidate)
        limit = query.limit
        selected = [c for group in by_kind.values() for c in group[:limit]]
        selected.sort(key=_sort_key)
        result_limit_reached = any(len(group) > limit for group in by_kind.values()) or (
            semantic_done is True and len(semantic_payloads) >= limit * len(query.kinds)
        )

        # 摘要配额：全局排序后每类前 SUMMARY_CARDS_PER_KIND 张附带正式摘要并登记 read_set
        # 其余只给标题卡片（read_handle=None）——扩大候选广度的同时压住 token，标题卡片值得深入时经 read_memories 取全文
        kind_seen: dict[str, int] = {}
        items = []
        for c in selected:
            payload = self._pin_payload(c["payload"])
            kind = payload["kind"]
            kind_seen[kind] = kind_seen.get(kind, 0) + 1
            card_handle = session.register_candidate(payload["document_id"])
            if kind_seen[kind] > SUMMARY_CARDS_PER_KIND:
                items.append(SearchItem(
                    card_handle=card_handle,
                    read_handle=None,
                    kind=kind,
                    title=payload["title"],
                    matched_by=c["matched_by"],
                    time_context=self._time_context(payload),
                    sections={},
                ))
                continue
            sections = self._summary_sections(payload)
            read_handle = session.register_read(
                payload["document_id"], sections, payload["revision"], payload["content_hash"],
                kind=kind, title=payload["title"],
                conversation_time=payload["conversation_time"],
                time_context=self._time_context(payload).model_dump(mode='json'),
            )
            items.append(SearchItem(
                card_handle=card_handle,
                read_handle=read_handle,
                kind=kind,
                title=payload["title"],
                matched_by=c["matched_by"],
                time_context=self._time_context(payload),
                sections=sections,
            ))
            session.record_retrievals([self._retrieval_entry(payload, card_handle, read_handle, sections)])

        return SearchResult(
            status=Status.PARTIAL if errors else Status.OK,
            coverage=SearchCoverage(
                scope_handle=query.scope_handle,
                literal_scan_complete=literal_done,
                semantic_query_completed=semantic_done,
                result_limit_reached=result_limit_reached,
                response_budget_reached=False,  # token 预算由 agent 记账后接入
                temporal=time_info,
            ),
            items=items,
            errors=errors,
        )


    async def read(self, query: ReadInput, session: ReadLedger) -> ReadResult:
        """按本attempt固定的正式正文读取全文；只登记实际提供的公开分区。"""
        reads: list[ReadEntry] = []
        errors: list[ReadError] = []
        for card_handle in query.card_handles:
            document_id = session.cards.get(card_handle)
            if document_id is None:
                errors.append(ReadError(
                    code="unknown_card_handle", card_handle=card_handle,
                    message=f"本轮不存在卡片句柄 {card_handle}；只能使用本轮工具返回的 card_handle。",
                ))
                continue
            payload = self._pinned_documents.get(document_id) or await self._load_payload(document_id)
            if payload is None:
                errors.append(ReadError(
                    code="document_missing", card_handle=card_handle,
                    message=f"文档 {document_id} 已不存在。",
                ))
                continue
            if not self._in_scope(payload):
                errors.append(ReadError(
                    code="hash_mismatch", card_handle=card_handle,
                    message=f"文档 {document_id} 的内容 hash 与登记值不符，已拒绝读取。",
                ))
                continue
            payload = self._pin_payload(payload)
            if self._tokens(payload["sections"]) > SINGLE_DOC_TOKEN_LIMIT:
                errors.append(ReadError(
                    code="document_too_large", card_handle=card_handle,
                    message=f"文档 {document_id} 超过单篇 {SINGLE_DOC_TOKEN_LIMIT} token 上限。",
                ))
                continue
            # 同篇同版本同覆盖范围已读过：复用原句柄，不重复登记
            read_handle = next(
                (handle for handle, entry in session.read_set.items()
                 if entry["document_id"] == document_id
                 and entry["revision"] == payload["revision"]
                 and entry["sections"] == payload["sections"]),
                None,
            )
            if read_handle is None:
                read_handle = session.register_read(
                    document_id, payload["sections"], payload["revision"], payload["content_hash"],
                    kind=payload["kind"], title=payload["title"],
                    conversation_time=payload["conversation_time"],
                    time_context=self._time_context(payload).model_dump(mode='json'),
                )
            reads.append(ReadEntry(
                read_handle=read_handle,
                card_handle=card_handle,
                kind=payload["kind"],
                title=payload["title"],
                sections=payload["sections"],
                time_context=self._time_context(payload),
            ))
            # 实际read返回全文，近期缓存也保留完整可见正文
            session.record_retrievals([
                self._retrieval_entry(payload, card_handle, read_handle, payload["sections"])
            ])
        return ReadResult(status=Status.PARTIAL if errors else Status.OK, reads=reads, errors=errors)


    async def list_memories(self, query: ListInput, session: ReadLedger) -> ListResult:
        """按模式选择召回池，再按时间和标题或预读分区筛选。"""
        time_range = query.time_range  # TimeRange | None，start/end 已是带时区 datetime

        payloads, time_info = await self._time_candidates(
            "hot" if query.mode == "overview" else "cold",
            query.kinds, time_range, query.unknown_event_time)
        # 纯筛选排序，不是向量检索
        def _kw_hit(payload: dict) -> bool:
            return MemoryReadService._match_hit(payload["match_text"], query.keywords, query.match) if query.keywords else True

        matched = [p for p in payloads if _kw_hit(p)]

        if query.mode == "fallback" and query.keywords and query.match == "any":
            matched.sort(key=lambda p: (-MemoryReadService._occurrence_count(p["match_text"], query.keywords),
                                        -MemoryReadService._time_key(p), p["document_id"]))
        else:
            matched.sort(key=lambda p: (-MemoryReadService._time_key(p), p["document_id"]))

        result_limit_reached = len(matched) > query.limit
        matched = matched[:query.limit]

        items = []
        for payload in matched:
            payload = self._pin_payload(payload)
            sections = self._summary_sections(payload)
            card_handle = session.register_candidate(payload["document_id"])
            read_handle = session.register_read(
                payload["document_id"], sections, payload["revision"], payload["content_hash"],
                kind=payload["kind"], title=payload["title"],
                conversation_time=payload["conversation_time"],
                time_context=self._time_context(payload).model_dump(mode='json'),
            )
            items.append(ListItem(
                card_handle=card_handle, read_handle=read_handle,
                kind=payload["kind"], title=payload["title"],
                time_context=self._time_context(payload), sections=sections,
            ))
            session.record_retrievals([self._retrieval_entry(payload, card_handle, read_handle, sections)])

        return ListResult(
            status=Status.OK,
            coverage=ListCoverage(
                scope_handle=query.scope_handle,
                scan_complete=True,
                result_limit_reached=result_limit_reached,
                temporal=time_info,
            ),
            items=items,
        )


    @staticmethod
    def _summary_sections(payload: dict[str, Any]) -> dict[str, Any]:
        """daily提供五个公开分区；topic预读主题说明和当前状态，时间线按需读取。"""
        sections = payload["sections"]
        if payload["kind"] == "daily":
            keys = ("memorable_moments", "profile_facts", "intentions", "commitments", "reflections")
        elif payload["kind"] == "topic":
            keys = ("topic_identity", "current_state")
        else:
            return {}
        return {key: sections[key] for key in keys if key in sections}


    def _time_context(self, payload: dict) -> TimeContext | TopicTimeContext:
        """daily仅听说时间；topic增加明确事件时间，未知不猜。"""
        return RecallDocumentSnapshot.time_context(payload)

    async def _document_snapshot(self) -> RecallDocumentSnapshot:
        if self._snapshot is None:
            documents = await self._documents.load(self.character_id)
            self._snapshot = RecallDocumentSnapshot(documents)
        return self._snapshot

    async def _semantic_recall(self, query: str, candidates: list[dict], limit: int) -> list[dict]:
        formal = {p["document_id"]: p for p in candidates}
        hits = await self._search.search(query, character_id=self.character_id,
            refs=tuple(p["_ref"] for p in candidates), limit=limit)
        result, seen = [], set()
        for hit in hits:
            payload = formal.get(hit.ref.memory_id)
            if payload is not None and payload["_ref"] == hit.ref and hit.ref.memory_id not in seen:
                result.append(payload)
                seen.add(hit.ref.memory_id)
        return result

    def _retrieval_entry(self, payload: dict, card_handle: str, read_handle: str,
                         sections: dict[str, Any]) -> RecentRecord:
        """构造近期已读条目（record_retrievals 的输入形态）。"""
        return {
            "document_id": payload["document_id"],
            "kind": payload["kind"],
            "card_handle": card_handle,
            "read_handle": read_handle,
            "title": payload["title"],
            "sections": sections,
            "revision": payload["revision"],
            "content_hash": payload["content_hash"],
            "conversation_time": payload["conversation_time"],
            "time_context": self._time_context(payload).model_dump(mode='json'),
        }


    def _tokens(self, obj: Any) -> int:
        """Count serialized document sections with the configured tokenizer."""
        return RecallMaterialBudget.count_tokens(obj, self._token_counter)
