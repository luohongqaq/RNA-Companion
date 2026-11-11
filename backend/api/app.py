"""FastAPI application factory and route composition."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.exception_handlers import register_exception_handlers
from api.lifespan import lifespan
from api.logging_middleware import RequestLoggingMiddleware
from api.routes import conversations, events, generation, memories, memory_edits, messages, system, characters, model_settings


def create_app() -> FastAPI:
    application = FastAPI(
        title="RNA Companion Backend",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        # app://rna 是 Electron 生产环境页面的自定义协议来源；file:// 页面的 Origin 为 null，
        # 无法通过白名单，因此生产构建改用自定义协议加载前端。
        allow_origins=["app://rna", "http://localhost", "http://127.0.0.1"],
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(application)
    application.add_middleware(RequestLoggingMiddleware)
    application.include_router(system.router)
    application.include_router(conversations.router, prefix="/api/v1")
    application.include_router(messages.router, prefix="/api/v1")
    application.include_router(generation.router, prefix="/api/v1")
    application.include_router(memories.router, prefix="/api/v1")
    application.include_router(memory_edits.router, prefix="/api/v1")
    application.include_router(events.router, prefix="/api/v1")
    application.include_router(characters.router, prefix="/api/v1")
    application.include_router(model_settings.router, prefix="/api/v1")
    return application


app = create_app()

