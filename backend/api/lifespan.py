"""Application startup and shutdown lifecycle."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI

from api.container import build_container
from logger import get_logger
from storage.config.settings import Settings
from storage.sqlite.log_sink import LoggingRuntime

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    runtime = await asyncio.to_thread(
        LoggingRuntime, settings.log_database_path, level=settings.log_level,
        retention_days=settings.log_retention_days,
    )
    application.state.logging_runtime = runtime
    try:
        log.info("应用启动", extra={"stage": "application", "event": "startup_started"})
        container = build_container(settings)
        application.state.container = container
        await container.start()
        try:
            log.info("应用已就绪", extra={"stage": "application", "event": "startup_finished"})
            yield
        finally:
            await container.stop()
    except Exception:
        log.error("应用生命周期异常", exc_info=True, extra={"stage": "application", "event": "lifecycle_failed"})
        raise
    finally:
        log.info("应用已停止", extra={"stage": "application", "event": "shutdown_finished"})
        await asyncio.to_thread(runtime.close)

