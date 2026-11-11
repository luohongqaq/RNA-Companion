"""旧任务升级后保留结构化检查点，不再依赖 Markdown。"""
from dataclasses import replace
from pathlib import Path
import json
import sqlite3

import pytest
from sqlalchemy import select

from module.memory_management.daily.core.domain.publication import DailyWrite
from module.memory_management.topic.core.domain.publication import TopicWriteItem
from module.memory_management.shared.jobs.repository import mappers as job_mappers
from storage.sqlite import db_session
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import MemoryJobRow
from test_memory_job_mapping import make_job


@pytest.mark.asyncio
async def test_migration_removes_text_copies_and_preserves_checkpoints(tmp_path):
    path = tmp_path / "old.sqlite3"
    baseline = Path(db_session.__file__).with_name("migrations") / "legacy" / "001_initial.sql"
    with sqlite3.connect(path) as connection:
        connection.executescript(baseline.read_text(encoding="utf-8"))
        connection.executescript("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP); INSERT INTO schema_migrations(version) VALUES (1);")
    database = SQLiteDatabase(path)
    expected = {}
    try:
        async with database.transaction() as session:
            for kind in ("daily", "topic", "update"):
                job = replace(make_job(kind), job_id=kind, target_key=kind, dedupe_key=kind)
                if kind == "daily":
                    job = replace(job, write_result=DailyWrite.from_document(job.final_document()))
                else:
                    job = replace(job, write_results=tuple(TopicWriteItem.from_application(app) for app in job.final_applications()))
                expected[kind] = job
                row = job_mappers.memory_job_to_row(job)
                data = row.payload_json
                data.update(schema_version=4, exported_artifact={"path": "old.md"}, exported_artifacts=[])
                data["vector_document"]["daily_memory"] = "old Markdown"
                if kind == "daily":
                    data["write_result"]["markdown"] = "old Markdown"
                else:
                    for item in data["write_results"]:
                        item["write"]["markdown"] = "old Markdown"
                session.add(row)
        await database.initialize()
        await database.initialize()
        async with database.db_session() as session:
            rows = (await session.scalars(select(MemoryJobRow))).all()
            assert len(rows) == 3
            for row in rows:
                assert job_mappers.memory_job_to_domain(row) == expected[row.job_id]
                assert '"markdown"' not in json.dumps(row.payload_json)
                assert '"daily_memory"' not in json.dumps(row.payload_json)
                assert "exported_artifact" not in row.payload_json
        with sqlite3.connect(path) as connection:
            for table in ("daily_memories", "topic_memories"):
                assert "markdown_path" not in {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        await database.close()
