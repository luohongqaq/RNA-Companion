"""记录 HTTP 生命周期，不读取请求体、鉴权头或查询字符串。"""
from time import perf_counter
from uuid import uuid4

from logger import get_logger, log_context

log = get_logger(__name__)


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start = perf_counter()
        request_id = uuid4().hex
        status_code = 500

        async def logged_send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message["headers"] = [*message.get("headers", []), (b"x-request-id", request_id.encode())]
            await send(message)

        with log_context(component="foreground", stage="http", request_id=request_id,
                         method=scope["method"]):
            log.info("HTTP 请求开始", extra={"event": "request_started"})
            try:
                await self.app(scope, receive, logged_send)
            except Exception:
                log.error("HTTP 请求异常", exc_info=True, extra={"event": "request_failed"})
                raise
            finally:
                route = getattr(scope.get("route"), "path", "unmatched")
                identifiers = {key: value for key, value in scope.get("path_params", {}).items()
                               if key in ("conversation_id", "job_id")}
                log.log(40 if status_code >= 500 else 30 if status_code >= 400 else 20,
                        "HTTP 请求结束", extra={"event": "request_finished", "route": route,
                        "status_code": status_code, "duration_ms": (perf_counter() - start) * 1000,
                        **identifiers})
