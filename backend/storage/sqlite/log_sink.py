"""独立 SQLite 日志库；业务线程只入队，单独线程批量写入。"""
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from queue import Queue
import sqlite3
import sys
from threading import Thread
import traceback

from logger import current_context

_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL, level TEXT NOT NULL, logger_name TEXT NOT NULL,
    file_path TEXT NOT NULL, line_number INTEGER NOT NULL, function_name TEXT NOT NULL,
    component TEXT, stage TEXT, event TEXT, request_id TEXT, conversation_id TEXT,
    job_id TEXT, attempt_id TEXT, duration_ms REAL, status_code INTEGER,
    message TEXT NOT NULL, exception TEXT, context_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_logs_time ON logs(timestamp);
CREATE INDEX IF NOT EXISTS ix_logs_job ON logs(job_id, id);
CREATE INDEX IF NOT EXISTS ix_logs_request ON logs(request_id, id);
"""
_INSERT = "INSERT INTO logs(timestamp,level,logger_name,file_path,line_number,function_name,component,stage,event,request_id,conversation_id,job_id,attempt_id,duration_ms,status_code,message,exception,context_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
_STOP = object()


class SQLiteLogHandler(logging.Handler):
    def __init__(self, path: Path, *, level: str = "INFO", retention_days: int = 30):
        super().__init__(level)
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue = Queue()
        self._stopped = False
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="milliseconds")
            connection.execute("DELETE FROM logs WHERE julianday(timestamp) < julianday(?)", (cutoff,))
        self._thread = Thread(target=self._write, name="sqlite-log-writer", daemon=True)
        self._thread.start()

    def emit(self, record):
        if self._stopped:
            return
        try:
            context = current_context()
            for key in ("component", "stage", "event", "request_id", "conversation_id", "job_id",
                        "attempt_id", "duration_ms", "status_code", "method", "route", "result",
                        "message_request_id"):
                if hasattr(record, key):
                    context[key] = getattr(record, key)
            # 保留异常类型和源码栈，不记录可能含密钥、正文的异常字符串或局部变量。
            exception = None
            if record.exc_info and record.exc_info[0]:
                exception = record.exc_info[0].__name__ + "\n" + "\n".join(
                    f'File "{frame.filename}", line {frame.lineno}, in {frame.name}'
                    for frame in traceback.extract_tb(record.exc_info[2]))
            timestamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds")
            self._queue.put_nowait((
                timestamp, record.levelname, record.name,
                getattr(record, "source_path", record.pathname),
                getattr(record, "source_line", record.lineno),
                getattr(record, "source_function", record.funcName),
                *(context.get(key) for key in ("component", "stage", "event", "request_id",
                  "conversation_id", "job_id", "attempt_id", "duration_ms", "status_code")),
                record.getMessage(), exception, json.dumps(context, ensure_ascii=False, default=str),
            ))
        except Exception:
            self.handleError(record)

    def _write(self):
        connection = None
        try:
            while True:
                item = self._queue.get()
                batch = [item]
                stop = item is _STOP
                while not stop and len(batch) < 100 and not self._queue.empty():
                    item = self._queue.get()
                    batch.append(item)
                    stop = item is _STOP
                rows = [item for item in batch if item is not _STOP]
                try:
                    if rows:
                        if connection is None:
                            connection = sqlite3.connect(self.path, timeout=5)
                        connection.executemany(_INSERT, rows)
                        connection.commit()
                except Exception as error:
                    if connection is not None:
                        connection.rollback()
                    print(f"SQLite 日志写入失败: {type(error).__name__}，丢失 {len(rows)} 条", file=sys.stderr)
                finally:
                    for _ in batch:
                        self._queue.task_done()
                if stop:
                    return
        finally:
            if connection is not None:
                connection.close()

    def flush(self):
        if hasattr(self, "_queue"):
            self._queue.join()

    def close(self):
        if not hasattr(self, "_thread"):
            return super().close()
        self.acquire()
        try:
            if not self._stopped:
                self._stopped = True
                self._queue.put_nowait(_STOP)
        finally:
            self.release()
        self._thread.join()
        super().close()


class ApplicationLogFilter(logging.Filter):
    def filter(self, record):
        return record.name.split(".")[0] in {"api", "module", "storage", "logger", "server"}


class LoggingRuntime:
    def __init__(self, path: Path, *, level="INFO", retention_days=30):
        self.handler = SQLiteLogHandler(path, level=level, retention_days=retention_days)
        self.handler.addFilter(ApplicationLogFilter())
        self.root = logging.getLogger()
        self.previous_level = self.root.level
        self.root.setLevel(min(self.root.level, logging.getLevelNamesMapping()[level]))
        self.root.addHandler(self.handler)

    def close(self):
        self.root.removeHandler(self.handler)
        self.handler.close()
        self.root.setLevel(self.previous_level)
