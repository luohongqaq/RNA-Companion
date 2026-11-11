"""单attempt句柄账本与固定材料；不接触具体数据库或模型SDK。"""
from __future__ import annotations
import copy
from module.memory_recall.retrieval.core.domain.ports import MemoryReadPort, ReadSessionSeed
from module.memory_recall.retrieval.core.domain.tools.listing import ListInput, ListResult
from module.memory_recall.retrieval.core.domain.tools.read import ReadInput, ReadResult
from module.memory_recall.retrieval.core.domain.tools.search import SearchInput, SearchResult
from module.memory_recall.shared.core.domain.policy import RECENT_LIMIT
from module.memory_recall.shared.core.domain.records import ReadRecord, RecentRecord
from typing import Any


class MemoryReadSession:
    def __init__(self, reader: MemoryReadPort, cache: ReadSessionSeed | None = None) -> None:
        self._reader = reader
        self._card_counter = 0
        self.cards: dict[str, str] = {}
        self._card_by_doc: dict[str, str] = {}
        self._read_counter = 0
        self.read_set: dict[str, ReadRecord] = {}
        self.recent_records: dict[str, RecentRecord] = {}
        self.updated_records: list[RecentRecord] = []
        if cache is not None:
            self.updated_records = [self._restore(record) for record in cache.recent_update_memory]
            self.recent_records = {record['document_id']: self._restore(record) for record in cache.recent_retrievals}

    def _restore(self, record: ReadRecord) -> RecentRecord:
        entry = copy.deepcopy(record)
        entry['card_handle'] = self.register_candidate(entry['document_id'])
        entry['read_handle'] = self.register_read(**copy.deepcopy(record))
        return entry

    async def has_documents(self) -> bool:
        return await self._reader.has_documents()

    async def temporal_coverage(self) -> dict:
        return await self._reader.temporal_coverage()

    async def search(self, query: SearchInput) -> SearchResult:
        return await self._reader.search(query, self)

    async def read(self, query: ReadInput) -> ReadResult:
        return await self._reader.read(query, self)

    async def list_memories(self, query: ListInput) -> ListResult:
        return await self._reader.list_memories(query, self)

    def record_retrievals(self, entries: list[RecentRecord]) -> None:
        """登记/更新近期实际展示的记忆正文分区。

        功能：按 document_id 去重并刷新最近使用顺序（弹出再写回，dict 的
        插入序即最近使用序）；超出 RECENT_LIMIT 时按完整条目淘汰最旧的，
        不截断正式分区后仍当完整正文。

        输入：entries——本次实际提供给模型的正文条目（document_id/kind/
        card_handle/read_handle/title/sections/revision/content_hash/
        conversation_time）。

        输出：无；状态保留在实例内，供 recent_retrievals 与跨轮核验使用。
        """
        for entry in entries:
            entry = copy.deepcopy(entry)
            document_id = entry["document_id"]
            previous = self.recent_records.get(document_id)
            if previous and (previous['revision'], previous['content_hash']) == (entry['revision'], entry['content_hash']):
                # 同版本再次搜索只展示摘要时，不丢掉此前实际读过的正文分区。
                entry['sections'] = {**copy.deepcopy(previous['sections']), **entry['sections']}
                registered = self.read_set.get(entry['read_handle'])
                if registered is not None and entry['sections'] != registered['sections']:
                    entry['read_handle'] = self.register_read(
                        document_id, entry['sections'], entry['revision'], entry['content_hash'],
                        kind=entry['kind'], title=entry['title'],
                        conversation_time=entry['conversation_time'], time_context=entry['time_context'],
                    )
            # 放到字典末尾
            self.recent_records.pop(document_id, None)
            self.recent_records[document_id] = copy.deepcopy(entry)
        while len(self.recent_records) > RECENT_LIMIT:
            # 淘汰最旧的一条
            self.recent_records.pop(next(iter(self.recent_records)))


    def recent_retrievals(self) -> list[RecentRecord]:
        """输出组装 L0 用的近期已读正文。

        功能：从旧到新输出最近读过的最多10张卡片及实际可见正文分区。
        read工具返回全文时缓存全文；search/list仅展示摘要时只缓存相应分区。
        不包含调用日志或错误轨迹，句柄只在本attempt有效。

        输入：无。

        输出：[{document_id, kind, card_handle, read_handle, title,
        sections, revision, content_hash, conversation_time}]；read_handle
        只覆盖登记时实际提供的正文分区。
        """
        return list(self.recent_records.values())


    def materialize(self, selected: list[dict]) -> list[dict]:
        """finish 交卷后的材料抽取。

        功能：校验 selected 的 read_handle/section_keys 均在本轮 read_set
        内实际核验过，从登记版本原样抽取正文（不采用模型自行编写的事实
        总结），附版本和来源进入冻结清单；质量标记和必要来源元数据随事实
        保留，防止丢失关键限定。

        输入：selected——finish_selection 已通过校验的
        [{read_handle, section_keys}]。

        输出：[{read_handle, document_id, kind, title, revision, source,
        sections}]；任一项校验失败抛 ValueError（汇总全部问题，不做部分
        抽取）——走到这一步说明 agent 的 finish 校验有漏洞，应当炸响而不是
        静默跳过。
        """
        problems = []
        materials = []
        for item in selected:
            read_handle = item.get("read_handle")
            entry = self.read_set.get(read_handle)
            if entry is None:
                problems.append(f"{read_handle} 不在本轮 read_set 中")
                continue
            section_keys = item.get("section_keys", [])
            missing = [key for key in section_keys if key not in entry["sections"]]
            if missing:
                problems.append(f"{read_handle} 未覆盖分区 {missing}，不能选择尚未读取的分区")
                continue
            materials.append({
                "read_handle": read_handle,
                "document_id": entry["document_id"],
                "kind": entry["kind"],
                "title": entry["title"],
                "revision": entry["revision"],
                "source": {"conversation_time": entry["conversation_time"],
                           "time_context": entry.get("time_context"),
                           "content_hash": entry["content_hash"]},
                "sections": {key: entry["sections"][key] for key in section_keys},
            })
        if problems:
            raise ValueError("；".join(problems))
        return materials


    def register_candidate(self, document_id: str) -> str:
        """对于检索到的document_id，签发句柄，向模型展示的card_handle而不是document_id
        对于已经签发过的document，直接card_handle
        """
        if document_id in self._card_by_doc:
            return self._card_by_doc[document_id]
        else:
            self._card_counter += 1
            card_handle = f"card_{self._card_counter:03d}"
            self.cards[card_handle] = document_id
            self._card_by_doc[document_id] = card_handle
            return card_handle


    def register_read(self, document_id: str, sections: dict[str, Any], revision: int,
                         content_hash: str, kind: str = "", title: str = "",
                         conversation_time: str | None = None,
                         time_context: dict | None = None) -> str:
        """对于阅读过的 document_id，签发句柄，向模型展示的 read_handle 而不是 document_id。

        read_set 的唯一写入入口，三个调用方（search 预读 / read 全文 /
        list 摘要）都必须经此登记。sections 只登记实际给模型的分区——
        read_handle 的覆盖范围以此为准，finish 只能选这里的键；kind/title/
        conversation_time 供 materialize 的冻结清单使用。
        """
        self._read_counter += 1
        read_handle = f"read_{self._read_counter:03d}"
        self.read_set[read_handle] = {
            "document_id": document_id,
            "kind": kind,
            "title": title,
            "sections": sections,
            "revision": revision,
            "content_hash": content_hash,
            "conversation_time": conversation_time,
            "time_context": time_context or {"conversation_time": conversation_time},
        }
        return read_handle
