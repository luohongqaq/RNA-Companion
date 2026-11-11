"""用户编辑表：现有库升级、快照持久化和数据库约束。"""
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow, MemoryEditRow

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def edit_row(**changes):
    values = dict(edit_id="edit-1", character_id="c", kind="daily", document_id="daily-1",
        idempotency_key="request-1", base_revision=2, base_content_hash="old-hash",
        revision=3, content_hash="new-hash", document_json={"title": "用户修改", "intentions": []},
        available_at=NOW, created_at=NOW, updated_at=NOW)
    return MemoryEditRow(**(values | changes))


@pytest_asyncio.fixture
async def database(tmp_path):
    database = SQLiteDatabase(tmp_path / "edits.db")
    try:
        await database.initialize()
        async with database.transaction() as session:
            session.add(CharacterRow(character_id="c", name="角色", character_setting="test"))
        yield database
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_upgrade_existing_database_is_repeatable_and_preserves_data(database):
    # 回到第三版结构，模拟已有数据的旧库启动迁移。
    async with database.transaction() as session:
        await session.execute(text("DROP TABLE memory_edits"))
        await session.execute(text("DELETE FROM schema_migrations WHERE version = 4"))
    await database.initialize()
    await database.initialize()
    async with database.db_session() as session:
        assert (await session.get(CharacterRow, "c")).name == "角色"
        assert await session.scalar(text("SELECT COUNT(*) FROM schema_migrations WHERE version = 4")) == 1
        connection = await session.connection()
        columns, indexes = await connection.run_sync(lambda conn: (
            {item["name"] for item in inspect(conn).get_columns("memory_edits")},
            {item["name"] for item in inspect(conn).get_indexes("memory_edits")},
        ))
        assert columns == set(MemoryEditRow.__table__.columns.keys())
        assert indexes == {"ix_memory_edits_ready", "ix_memory_edits_document"}
        assert not (await session.execute(text("PRAGMA foreign_key_check"))).all()


@pytest.mark.asyncio
async def test_edit_snapshot_and_index_progress_round_trip(database):
    async with database.transaction() as session:
        session.add(edit_row())
    async with database.transaction() as session:
        row = await session.get(MemoryEditRow, "edit-1")
        assert row.document_json == {"title": "用户修改", "intentions": []}
        assert (row.index_status, row.retry_count, row.attempt_version) == ("pending", 0, 0)
        row.index_status = "indexing"
        row.attempt_version += 1
    async with database.transaction() as session:
        row = await session.get(MemoryEditRow, "edit-1")
        row.index_status, row.indexed_at = "done", NOW
    async with database.db_session() as session:
        row = await session.get(MemoryEditRow, "edit-1")
        assert row.index_status == "done" and row.indexed_at == NOW
        assert row.revision == 3 and row.base_revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    dict(kind="object"), dict(character_id="missing"), dict(base_revision=0), dict(revision=5),
    dict(document_json=[]), dict(index_status="unknown"), dict(retry_count=-1), dict(attempt_version=-1),
    dict(index_status="done"), dict(indexed_at=NOW),
])
async def test_invalid_edit_is_rejected(database, changes):
    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            session.add(edit_row(**changes))


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    dict(document_id="different", kind="topic"),
    dict(idempotency_key="different"),
])
async def test_request_and_document_version_are_unique(database, changes):
    async with database.transaction() as session:
        session.add(edit_row())
    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            session.add(edit_row(edit_id="edit-2", **changes))
