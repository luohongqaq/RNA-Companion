"""完整基线重复初始化、事件约束与自增序号保留。"""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from module.chat.conversation.core.domain.contracts import SaveMessageInput
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from storage.sqlite import db_session
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow, ChatEventRow, MessageRow

@pytest.mark.asyncio
async def test_reinitialize_preserves_chat_and_enforces_event_constraints(tmp_path):
    path = tmp_path / "events.sqlite3"
    database = SQLiteDatabase(path)
    await database.initialize()
    try:
        async with database.transaction() as session:
            session.add(CharacterRow(character_id="c", name="test", character_setting="test"))
        saved = await SQLiteChatRepository(database).accept_message(SaveMessageInput(
            "request_event_migration", "conv", "c", "保留原消息",
        ))
        now = datetime.now(timezone.utc)
        async with database.transaction() as session:
            session.add(MessageRow(message_id="reply", turn_id=saved.turn_id,
                                   message_seq=2, role="character", content="正式回复", created_at=now))
        async with database.transaction() as session:
            await session.execute(text("""INSERT INTO chat_events
                (event_id,event_type,conversation_id,job_id,message_id,reply_json,created_at)
                VALUES (100,'reply_committed','conv',:job,'reply','{}',:now)"""),
                {"job": saved.job_id, "now": now.isoformat()})
            await session.execute(text("DELETE FROM chat_events"))
            await session.execute(text("""INSERT INTO chat_events
                (event_id,event_type,conversation_id,job_id,message_id,reply_json,created_at)
                VALUES (2,'reply_committed','conv',:job,'reply','{}',:now)"""),
                {"job": saved.job_id, "now": now.isoformat()})
            await session.execute(text("""INSERT INTO chat_events
                (event_id,event_type,conversation_id,job_id,failure_json,manual_retry_count,created_at)
                VALUES (3,'generation_failed','conv',:job,'{"saved": true}',0,:now)"""),
                {"job": saved.job_id, "now": now.isoformat()})
        await database.initialize()
        await database.initialize()
        async with database.db_session() as session:
            assert (await session.get(MessageRow, saved.message_id)).content == "保留原消息"
            assert (await session.get(MessageRow, "reply")).content == "正式回复"
            assert await session.scalar(select(func.count()).select_from(ChatEventRow)) == 2
            assert await session.scalar(text("SELECT MAX(version) FROM schema_migrations")) == 7
            assert not (await session.execute(text("PRAGMA foreign_key_check"))).all()
            assert await session.scalar(text("PRAGMA integrity_check")) == "ok"
            assert (await session.get(ChatEventRow, 2)).reply_json == {}
            assert (await session.get(ChatEventRow, 3)).failure_json == {"saved": True}
        async with database.transaction() as session:
            await session.execute(text("DELETE FROM chat_events"))

        def event(**overrides):
            fields = dict(event_type="reply_committed", conversation_id="conv", job_id=saved.job_id,
                          message_id="reply", reply_json={"content": "正式回复"}, created_at=now)
            return ChatEventRow(**{**fields, **overrides})

        async with database.transaction() as session:
            row = event()
            session.add(row)
            await session.flush()
            first_id = row.event_id
            assert first_id > 100
        for overrides in ({}, {"event_type": "unknown"}, {"message_id": "missing"},
                          {"reply_json": []}, {"reply_json": None}):
            with pytest.raises(IntegrityError):
                async with database.transaction() as session:
                    session.add(event(**overrides))
                    await session.flush()
        async with database.transaction() as session:
            await session.delete(await session.get(ChatEventRow, first_id))
        async with database.transaction() as session:
            row = event()
            session.add(row)
            await session.flush()
            assert row.event_id > first_id
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_complete_baseline_initializes_all_tables_and_is_repeatable(tmp_path):
    database = SQLiteDatabase(tmp_path / "baseline.sqlite3")
    try:
        await database.initialize()
        with sqlite3.connect(database.path) as connection:
            before = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        await database.initialize()
        with sqlite3.connect(database.path) as connection:
            assert connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == before
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"memory_objects", "daily_memories", "topic_memories", "chat_events", "sessions", "memory_recall_cache"} <= tables
            assert "object_candidates" not in tables
            assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
                (version,) for version in range(1, 8)
            ]
            assert connection.execute("SELECT seq FROM memory_update_clock").fetchall() == [(0,)]
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_baseline_matches_legacy_upgrade(tmp_path):
    fresh = SQLiteDatabase(tmp_path / "fresh.sqlite3")
    legacy = SQLiteDatabase(tmp_path / "legacy.sqlite3")
    initial = Path(db_session.__file__).with_name("migrations") / "legacy" / "001_initial.sql"
    with closing(sqlite3.connect(legacy.path)) as connection:
        connection.executescript(initial.read_text(encoding="utf-8"))
        connection.executescript("""
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO schema_migrations(version) VALUES (1);
        """)
    try:
        await fresh.initialize()
        await legacy.initialize()
        async with fresh.db_session() as new_session, legacy.db_session() as upgraded_session:
            schema = text("SELECT type, name, sql FROM sqlite_master "
                          "WHERE sql IS NOT NULL AND name NOT IN ('schema_migrations', 'characters') "
                          "ORDER BY type, name")
            # ALTER TABLE changes the stored CREATE TABLE formatting.
            character_columns = text("PRAGMA table_info(characters)")
            versions = text("SELECT version FROM schema_migrations ORDER BY version")
            clock = text("SELECT singleton, seq FROM memory_update_clock")
            for query in (schema, character_columns, versions, clock):
                assert (await new_session.execute(query)).all() == (await upgraded_session.execute(query)).all()
    finally:
        await fresh.close()
        await legacy.close()
