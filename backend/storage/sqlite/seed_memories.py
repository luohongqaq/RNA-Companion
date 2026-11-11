"""测试记忆的正式版本登记；原始聊天缺失时使用独立封存来源容器。"""
from collections.abc import Sequence
from datetime import date, datetime, timezone
import hashlib
import json
from sqlalchemy import bindparam, func, select, text
from module.shared.common.time import BackendTime
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow, ConversationRow, SessionRow


class SQLiteSeedMemoryRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def current_versions(self, document_ids: Sequence[str], character_id: str) -> dict[str, tuple[int, str]]:
        if not document_ids:
            return {}
        statement = text('''SELECT daily_id AS id, revision, content_hash FROM daily_memories
            WHERE character_id=:owner AND daily_id IN :ids
            UNION ALL SELECT topic_id AS id, revision, content_hash FROM topic_memories
            WHERE character_id=:owner AND topic_id IN :ids''').bindparams(bindparam('ids', expanding=True))
        async with self._database.db_session() as session:
            rows = (await session.execute(statement, {'owner': character_id, 'ids': list(document_ids)})).mappings()
            return {row['id']: (row['revision'], row['content_hash']) for row in rows}

    async def publish_seed(self, documents: Sequence[dict], character_id: str, namespace: str) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        marker = hashlib.sha256(f'{namespace}:{character_id}'.encode()).hexdigest()[:20]
        conversation_id = f'seed_source_{marker}'
        counts = {'created': 0, 'updated': 0, 'unchanged': 0}
        async with self._database.transaction() as session:
            if await session.get(CharacterRow, character_id) is None:
                raise ValueError('导入角色尚未登记')
            route = await session.get(ConversationRow, conversation_id)
            if route is None:
                session.add(ConversationRow(conversation_id=conversation_id, character_id=character_id,
                    title='记忆种子来源（未导入原始聊天）', state='archived',
                    created_at=now, updated_at=now, ended_at=now))
                await session.flush()
            for doc in documents:
                table, key = ('daily_memories', 'daily_id') if doc['kind'] == 'daily' else ('topic_memories', 'topic_id')
                old = (await session.execute(text(f'SELECT character_id, revision, content_hash FROM {table} WHERE {key}=:id'),
                    {'id': doc['document_id']})).mappings().first()
                if old and old['character_id'] != character_id:
                    raise ValueError('种子ID与其他角色现有记忆冲突')
                if old and (old['revision'], old['content_hash']) == (doc['revision'], doc['content_hash']):
                    counts['unchanged'] += 1
                    continue
                columns = 'character_id, revision, content_hash, recall_tier, created_at, updated_at, last_mentioned_at'
                values = ':owner, :revision, :hash, :tier, :now, :now, :retention_date'
                params = {'id': doc['document_id'], 'owner': character_id, 'revision': doc['revision'],
                    'hash': doc['content_hash'], 'tier': doc.get('pool', 'hot'), 'now': now.isoformat(),
                    'retention_date': BackendTime.local(now).date().isoformat()}
                extra_updates = ''
                if doc['kind'] == 'daily':
                    session_id = 'seed_session_' + hashlib.sha256(doc['document_id'].encode()).hexdigest()[:24]
                    heard = datetime.fromisoformat(doc['conversation_time'])
                    if heard.utcoffset() is None:
                        raise ValueError('种子conversation_time必须带时区')
                    memory_date = (date.fromisoformat(doc['memory_date']) if doc.get('memory_date')
                                   else BackendTime.local(heard).date())
                    if await session.get(SessionRow, session_id) is None:
                        session.add(SessionRow(session_id=session_id, conversation_id=conversation_id,
                            session_seq=(await session.scalar(select(func.max(SessionRow.session_seq)).where(
                                SessionRow.conversation_id == conversation_id)) or 0) + 1,
                            session_state='sealed', session_start_time=heard, session_end_time=heard,
                            daily_jobs_registered_at=now))
                        await session.flush()
                    document = dict(memory_date=memory_date.isoformat(), source_session_ids=[session_id],
                        title=doc['title'], memorable_moments=doc['sections']['memorable_moments'],
                        profile_facts=doc['sections']['profile_facts'], intentions=doc['sections']['intentions'],
                        commitments=doc['sections']['commitments'], reflections=doc['sections']['reflections'])
                    columns += ', memory_date, source_session_ids_json, document_json, vector_document_key, published_at'
                    values += ', :date, :sources, :document, :vector_key, :now'
                    params.update(date=memory_date.isoformat(), sources=json.dumps([session_id]),
                        document=json.dumps(document, ensure_ascii=False),
                        vector_key=doc.get('vector_document_key', doc['document_id']))
                    extra_updates = ''', memory_date=excluded.memory_date,
                        source_session_ids_json=excluded.source_session_ids_json,
                        document_json=excluded.document_json,
                        vector_document_key=excluded.vector_document_key, published_at=excluded.published_at'''
                else:
                    if "document" not in doc:
                        raise ValueError("topic种子需要完整的当前结构化正文")
                    document = doc["document"]
                    evidence = list(document["topic_identity"]["source_refs"])
                    for event in document["entries"]:
                        for ref in event["source_refs"]:
                            if ref not in evidence:
                                evidence.append(ref)
                    columns += ', document_json, source_refs_json, vector_document_key, published_at'
                    values += ', :document, :refs, :vector_key, :now'
                    params.update(document=json.dumps(document, ensure_ascii=False), refs=json.dumps(evidence, ensure_ascii=False),
                        vector_key=doc["vector_document_key"])
                    extra_updates = ", document_json=excluded.document_json, source_refs_json=excluded.source_refs_json, vector_document_key=excluded.vector_document_key, published_at=excluded.published_at"
                await session.execute(text(f'''INSERT INTO {table} ({key}, {columns}) VALUES (:id, {values})
                    ON CONFLICT({key}) DO UPDATE SET revision=excluded.revision, content_hash=excluded.content_hash,
                        recall_tier=excluded.recall_tier,
                        last_mentioned_at=COALESCE({table}.last_mentioned_at, excluded.last_mentioned_at),
                        updated_at=excluded.updated_at{extra_updates}'''), params)
                counts['updated' if old else 'created'] += 1
        return counts
