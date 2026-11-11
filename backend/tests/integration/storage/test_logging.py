"""SQLite 持久日志、协程上下文隔离和应用生命周期。"""
import asyncio
import inspect
import json
import logging
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from api.app import create_app
from logger import get_logger, log_context, stage
from storage.sqlite.log_sink import LoggingRuntime, SQLiteLogHandler


def read_logs(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM logs ORDER BY id")]


def test_log_source_exception_and_close_drains_queue(tmp_path):
    path = tmp_path / "logs.sqlite3"
    handler = SQLiteLogHandler(path)
    log = get_logger("module.test_logging")
    previous = log.level
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    try:
        with log_context(job_id="job", stage="test"):
            expected_line = inspect.currentframe().f_lineno + 1
            log.info("hello", extra={"event": "example"})
            try:
                raise ValueError("SECRET_DO_NOT_LOG")
            except ValueError:
                log.error("operation failed", exc_info=True)
        for index in range(120):
            log.info("queued %s", index)
    finally:
        log.removeHandler(handler)
        handler.close()
        handler.close()
        log.setLevel(previous)
    rows = read_logs(path)
    assert len(rows) == 122
    assert rows[0]["line_number"] == expected_line
    assert Path(rows[0]["file_path"]) == Path(__file__)
    assert rows[0]["job_id"] == "job"
    assert rows[0]["timestamp"].endswith("+00:00")
    assert rows[1]["exception"].startswith("ValueError")
    assert "SECRET_DO_NOT_LOG" not in str(rows)
    assert rows[-1]["job_id"] is None


@pytest.mark.asyncio
async def test_stage_context_isolated_and_errors_propagate(tmp_path):
    path = tmp_path / "logs.sqlite3"
    runtime = LoggingRuntime(path)

    async def operation(job_id, fail=False):
        await asyncio.sleep(0)
        if fail:
            raise ValueError("SECRET")
        return True
    operation.__module__ = "module.test_logging"
    traced = stage("test.operation", background=True)(operation)
    try:
        with log_context(request_id="unrelated", component="foreground"):
            results = await asyncio.gather(traced("one"), traced("two", fail=True), return_exceptions=True)
        assert results[0] is True and isinstance(results[1], ValueError)
    finally:
        await asyncio.to_thread(runtime.close)
    rows = read_logs(path)
    assert len(rows) == 4
    assert {row["job_id"] for row in rows} == {"one", "two"}
    assert all(row["request_id"] is None and row["component"] == "background" for row in rows)
    assert [(r["job_id"], r["event"]) for r in rows if r["event"] != "stage_started"] == [
        ("one", "stage_finished"), ("two", "stage_failed"),
    ]
    assert all(row["duration_ms"] >= 0 for row in rows if row["event"] != "stage_started")


@pytest.mark.asyncio
async def test_http_and_lifespan_log_without_sensitive_request_data(tmp_path, monkeypatch):
    import api.lifespan as lifespan_module
    path = tmp_path / "logs.sqlite3"
    container = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(lifespan_module, "Settings", lambda: SimpleNamespace(
        log_database_path=path, log_level="INFO", log_retention_days=30,
    ))
    monkeypatch.setattr(lifespan_module, "build_container", lambda settings: container)
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health?secret=DO_NOT_SAVE", headers={"Authorization": "DO_NOT_SAVE"})
            assert response.status_code == 200
            request_id = response.headers["x-request-id"]
            assert (await client.get("/does-not-exist")).status_code == 404
    rows = read_logs(path)
    request_rows = [row for row in rows if row["request_id"] == request_id]
    assert [row["event"] for row in request_rows] == ["request_started", "request_finished"]
    assert request_rows[-1]["status_code"] == 200
    assert json.loads(request_rows[-1]["context_json"])["route"] == "/health"
    assert "DO_NOT_SAVE" not in str(rows)
    assert rows[0]["event"] == "startup_started" and rows[-1]["event"] == "shutdown_finished"
    container.stop.assert_awaited_once()


def test_retention_and_reopen(tmp_path):
    path = tmp_path / "logs.sqlite3"
    runtime = LoggingRuntime(path)
    get_logger("module.test_logging").info("old entry")
    runtime.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE logs SET timestamp='2000-01-01T00:00:00.000+08:00'")
    runtime = LoggingRuntime(path, retention_days=30)
    get_logger("module.test_logging").info("new entry")
    runtime.close()
    assert [row["message"] for row in read_logs(path)] == ["new entry"]


@pytest.mark.asyncio
async def test_failed_startup_closes_logging_runtime(tmp_path, monkeypatch):
    import api.lifespan as lifespan_module
    path = tmp_path / "startup.sqlite3"
    container = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("private config")))
    monkeypatch.setattr(lifespan_module, "Settings", lambda: SimpleNamespace(
        log_database_path=path, log_level="INFO", log_retention_days=30,
    ))
    monkeypatch.setattr(lifespan_module, "build_container", lambda settings: container)
    app = create_app()
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            pass
    assert not app.state.logging_runtime.handler._thread.is_alive()
    rows = read_logs(path)
    assert any(row["event"] == "lifecycle_failed" for row in rows)
    assert "private config" not in str(rows)


@pytest.mark.asyncio
async def test_cancelled_stage_is_logged_and_not_swallowed(tmp_path):
    path = tmp_path / "cancel.sqlite3"
    runtime = LoggingRuntime(path)

    async def operation(job_id):
        raise asyncio.CancelledError
    operation.__module__ = "module.test_logging"
    try:
        with pytest.raises(asyncio.CancelledError):
            await stage("cancel")(operation)("cancelled_job")
    finally:
        await asyncio.to_thread(runtime.close)
    assert [row["event"] for row in read_logs(path)] == ["stage_started", "stage_cancelled"]
