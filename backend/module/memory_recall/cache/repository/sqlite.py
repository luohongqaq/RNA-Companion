"""SQLite更新信号与跨轮正文缓存；短事务内不访问Qdrant或模型。"""
from __future__ import annotations
import json
from module.memory_recall.cache.core.domain.cache import RecallCache, RecallCacheView
from module.memory_recall.cache.core.domain.update import MemoryUpdate
from module.memory_recall.shared.core.domain.policy import RECENT_LIMIT, RECENT_UPDATE_LIMIT
from sqlalchemy import text
from storage.sqlite.db_session import SQLiteDatabase


class SQLiteRecallCache:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def load(self, character_id: str, conversation_id: str) -> RecallCacheView:
        params = {'character': character_id, 'conversation': conversation_id}
        async with self._database.db_session() as db:
            row = (await db.execute(text('''SELECT version, signal_seq, payload_json FROM memory_recall_cache
                WHERE character_id=:character AND conversation_id=:conversation'''), params)).mappings().first()
            if row:
                payload = json.loads(row['payload_json'])
                if payload.get('schema_version') == 2:
                    cache = RecallCache(row['version'], row['signal_seq'],
                        payload['recent_update_memory'], payload['recent_retrievals'])
                else:
                    # 旧sections契约不可复用；保留CAS版本，重新读取正式正文和更新水位。
                    cache = RecallCache(version=row['version'])
            else:
                cache = RecallCache()
            latest = await db.scalar(text('''SELECT COALESCE(MAX(update_seq),0) FROM memory_update_signals
                WHERE character_id=:character'''), params)
            if latest == cache.signal_seq:
                return RecallCacheView(cache, latest)
            params['cursor'] = cache.signal_seq
            changes = (await db.execute(text('''SELECT * FROM memory_update_signals
                WHERE character_id=:character AND update_seq>:cursor ORDER BY update_seq'''), params)).mappings().all()
            params['limit'] = RECENT_UPDATE_LIMIT
            upstream = (await db.execute(text('''SELECT * FROM memory_update_signals
                WHERE character_id=:character AND deleted=0 ORDER BY update_seq DESC LIMIT :limit'''), params)).mappings().all()
            return RecallCacheView(cache, latest, tuple(self._update(r) for r in changes),
                                   tuple(self._update(r) for r in upstream))

    async def save(self, character_id: str, conversation_id: str, cache: RecallCache) -> bool:
        payload = json.dumps({'schema_version': 2, 'recent_update_memory': cache.recent_update_memory[:RECENT_UPDATE_LIMIT],
                              'recent_retrievals': cache.recent_retrievals[-RECENT_LIMIT:]}, ensure_ascii=False)
        params = {'character': character_id, 'conversation': conversation_id,
                  'version': cache.version, 'signal': cache.signal_seq, 'payload': payload}
        async with self._database.transaction() as db:
            result = await db.execute(text('''INSERT INTO memory_recall_cache
                (character_id, conversation_id, version, signal_seq, payload_json)
                SELECT :character, :conversation, 1, :signal, :payload WHERE :version=0
                ON CONFLICT(character_id, conversation_id) DO NOTHING'''), params)
            if result.rowcount == 1:
                cache.version = 1
                return True
            result = await db.execute(text('''UPDATE memory_recall_cache
                SET version=version+1, signal_seq=:signal, payload_json=:payload
                WHERE character_id=:character AND conversation_id=:conversation AND version=:version'''), params)
            if result.rowcount != 1:
                return False
            cache.version += 1
            return True

    @staticmethod
    def _update(row) -> MemoryUpdate:
        return MemoryUpdate(row['document_id'], row['kind'], row['revision'], row['content_hash'], bool(row['deleted']))
