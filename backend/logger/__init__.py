"""应用日志入口：仅依赖标准库，SQLite 写入实现由启动入口装配。"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import logging
from time import perf_counter

_context: ContextVar[dict] = ContextVar("log_context", default={})


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def current_context() -> dict:
    return dict(_context.get())


@contextmanager
def log_context(**fields):
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def stage(name: str, *, background: bool = False):
    """记录异步阶段开始、结束、耗时和异常；不记录函数参数正文。"""
    def decorate(function):
        signature = inspect.signature(function)
        log = get_logger(function.__module__)
        source = {"source_path": function.__code__.co_filename,
                  "source_line": function.__code__.co_firstlineno,
                  "source_function": function.__name__}

        @wraps(function)
        async def wrapped(*args, **kwargs):
            arguments = signature.bind_partial(*args, **kwargs).arguments
            fields = {"stage": name}
            if background:
                fields.update(component="background", request_id=None)
            for key in ("job_id", "attempt_id", "conversation_id"):
                if key in arguments:
                    fields[key] = arguments[key]
            for key in ("ticket", "command", "task", "job"):
                value = arguments.get(key)
                if value is not None:
                    for identifier in ("job_id", "attempt_id", "conversation_id", "request_id"):
                        item = getattr(value, identifier, None)
                        if item is not None:
                            fields["message_request_id" if identifier == "request_id" else identifier] = item
            start = perf_counter()
            with log_context(**fields):
                log.info("阶段开始", extra={**source, "event": "stage_started"})
                try:
                    result = await function(*args, **kwargs)
                except asyncio.CancelledError:
                    log.info("阶段取消", extra={**source, "event": "stage_cancelled",
                             "duration_ms": (perf_counter() - start) * 1000})
                    raise
                except Exception:
                    log.error("阶段异常", exc_info=True, extra={**source, "event": "stage_failed",
                              "duration_ms": (perf_counter() - start) * 1000})
                    raise
                log.info("阶段结束", extra={**source, "event": "stage_finished",
                         "duration_ms": (perf_counter() - start) * 1000,
                         "result": result if isinstance(result, bool) else None})
                return result
        return wrapped
    return decorate
