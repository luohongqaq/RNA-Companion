"""分区键改名同步 hash，不能改写正文中的同名字样。"""
import hashlib
import json
import sqlite3

import pytest

from module.memory_management.daily.core.domain.document import DailyDocument
from storage.sqlite.db_session import SQLiteDatabase


@pytest.mark.asyncio
async def test_old_daily_partition_and_refs_migrate_idempotently(tmp_path):
    database = SQLiteDatabase(tmp_path / "daily.sqlite3")
    try:
        await database.initialize()
        old = dict(memory_date="2026-11-04", source_session_ids=[], title="含 open_loops 的原标题",
            memorable_moments=[], profile_facts=[], commitments=[], reflections=[], open_loops=[
                dict(owner="user", certainty="plan", status="open", text="正文 open_loops 不要替换",
                     target_date=None, source_turn_ids=[], evidence_quotes=[])])
        old_hash = hashlib.sha256(json.dumps(old, ensure_ascii=False,sort_keys=True,separators=(",", ":")).encode()).hexdigest()
        with sqlite3.connect(database.path) as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version=3")
            connection.execute("INSERT INTO characters(character_id,name,character_setting) VALUES ('c','c','test')")
            connection.execute("""INSERT INTO daily_memories(daily_id,character_id,revision,content_hash,recall_tier,
                created_at,updated_at,memory_date,source_session_ids_json,document_json,vector_document_key,published_at)
                VALUES ('d','c',2,?,'hot',?,?,?,?,?,?,?)""", (old_hash,'2026-11-04T00:00:00+00:00',
                '2026-11-04T00:00:00+00:00','2026-11-04','[]',json.dumps(old),f'd:r2:{old_hash}','2026-11-04T00:00:00+00:00'))
        await database.initialize()
        await database.initialize()
        with sqlite3.connect(database.path) as connection:
            raw, new_hash, key = connection.execute('SELECT document_json,content_hash,vector_document_key FROM daily_memories').fetchone()
            data = json.loads(raw)
            assert "intentions" in data and "open_loops" not in data
            assert data["intentions"][0]["text"] == old["open_loops"][0]["text"]
            assert data["title"] == old["title"]
            assert DailyDocument.from_data(data).content_hash() == new_hash != old_hash
            assert key == f'd:r2:{new_hash}'
            assert connection.execute('SELECT content_hash FROM memory_update_signals WHERE document_id="d"').fetchone()[0] == new_hash
    finally:
        await database.close()
