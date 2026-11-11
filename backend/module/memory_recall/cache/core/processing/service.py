"""L0前按数据库信号刷新跨轮材料；没有更新时直接复用缓存正文。"""
from __future__ import annotations
import copy
from module.memory_recall.cache.core.domain.cache import RecallCache
from module.memory_recall.cache.core.domain.ports import RecallCachePort
from module.memory_recall.retrieval.core.domain.ports import MemoryReadPort


class MemoryCacheService:
    def __init__(self, repository: RecallCachePort) -> None:
        self._repository = repository

    async def prepare(self, character_id: str, conversation_id: str, reader: MemoryReadPort) -> RecallCache:
        view = await self._repository.load(character_id, conversation_id)
        cache = copy.deepcopy(view.cache)
        if view.latest_signal_seq == cache.signal_seq:
            return cache
        changes = {change.document_id: change for change in view.changes}
        wanted = {change.document_id: change for change in view.recent_updates}
        for record in cache.recent_retrievals:
            change = changes.get(record['document_id'])
            if change and not change.deleted:
                wanted[change.document_id] = change
        # 已缓存且版本匹配的正文直接复用，不重复按ID读取。
        existing = {r['document_id']: r for r in (*cache.recent_retrievals, *cache.recent_update_memory)}
        ready = {key: value for key, value in existing.items() if key in wanted and key not in changes
                 and (value['revision'], value['content_hash']) == (wanted[key].revision, wanted[key].content_hash)}
        missing = [key for key in wanted if key not in ready]
        loaded = await reader.load_records(missing) if missing else {}
        for key, record in loaded.items():
            expected = wanted.get(key)
            if expected and (record['revision'], record['content_hash'], record['kind']) == (
                    expected.revision, expected.content_hash, expected.kind):
                ready[key] = copy.deepcopy(record)
        cache.recent_retrievals = [copy.deepcopy(ready[r['document_id']]) if r['document_id'] in changes
                                   else r for r in cache.recent_retrievals
                                   if r['document_id'] not in changes or r['document_id'] in ready]
        # 上游5张的集合/版本未变化，保留原列表；检索行为不影响此栏。
        signature = lambda records: [(r['document_id'], r['revision'], r['content_hash']) for r in records]
        updated = [copy.deepcopy(ready[c.document_id]) for c in view.recent_updates if c.document_id in ready]
        if signature(updated) != signature(cache.recent_update_memory):
            cache.recent_update_memory = updated
        # load_records已从SQLite正式快照读取；缺失表示已删除、移出召回池或无可展示正文。
        # 若读到的版本与信号不一致，说明两次快照间又有更新，留待下轮重新核对。
        cache.refresh_complete = all(key not in loaded or key in ready for key in wanted)
        if cache.refresh_complete:
            cache.signal_seq = view.latest_signal_seq
            await self._repository.save(character_id, conversation_id, cache)
        return cache

    async def remember(self, character_id: str, conversation_id: str, cache: RecallCache, records: list[dict]) -> None:
        if not cache.refresh_complete:
            return
        # 只写实际展示材料；并发CAS失败时放弃迟到快照，不覆盖新attempt的更新水位。
        remembered = [{k: copy.deepcopy(v) for k, v in record.items()
                       if k not in ('card_handle', 'read_handle')} for record in records]
        if remembered == cache.recent_retrievals:
            return
        cache.recent_retrievals = remembered
        await self._repository.save(character_id, conversation_id, cache)
