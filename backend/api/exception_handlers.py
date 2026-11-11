"""Translate core exceptions into stable HTTP responses."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from module.shared.common.errors.base import BusinessError


def register_exception_handlers(application: FastAPI) -> None:
    @application.exception_handler(BusinessError)
    async def handle_business_error(
        _request: Request,
        exception: BusinessError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exception.status_code,
            content={"code": exception.code, "message": str(exception)},
        )

