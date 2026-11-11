"""重复初始化保留会话顺序，序号受正数和唯一约束保护。"""
import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from storage.sqlite.db_session import SQLiteDatabase


@pytest.mark.asyncio
async def test_session_sequences_survive_initialization(tmp_path):
    path = tmp_path / "old.sqlite3"
    database = SQLiteDatabase(path)
    await database.initialize()
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            INSERT INTO characters(character_id,name,character_setting) VALUES ('c','test','test');
            INSERT INTO conversations(conversation_id,character_id,state,created_at,updated_at)
            VALUES ('conv','c','active','2026-11-03T00:00:00+00:00','2026-11-03T00:00:00+00:00');
        """)
        # 时间、ID 顺序均不能替代前驱链。
        for seq, (sid, predecessor, started) in enumerate((
            ("z", None, "2026-11-03T00:00:00+00:00"),
            ("a", "z", "2026-11-02T00:00:00+00:00"),
            ("b", "a", "2026-11-02T00:00:00+00:00"),
        ), 1):
            connection.execute(
                "INSERT INTO sessions(session_id,conversation_id,predecessor_session_id,session_state,session_start_time,session_seq) "
                "VALUES (?,'conv',?,'sealed',?,?)", (sid, predecessor, started, seq),
            )
    try:
        await database.initialize()
        await database.initialize()
        async with database.db_session() as session:
            rows = (await session.execute(text(
                "SELECT session_id,session_seq FROM sessions ORDER BY session_seq"
            ))).all()
            assert rows == [("z", 1), ("a", 2), ("b", 3)]
        for seq in (0, 1):
            with pytest.raises(IntegrityError):
                async with database.transaction() as session:
                    await session.execute(text(
                        "UPDATE sessions SET session_seq=:seq WHERE session_id='b'"
                    ), {"seq": seq})
    finally:
        await database.close()
