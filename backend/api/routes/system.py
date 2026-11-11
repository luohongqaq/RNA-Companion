"""Health and lifecycle endpoints used by Electron."""

from fastapi import APIRouter, Request

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "architecture": "api-core-storage"}


@router.post("/shutdown")
async def shutdown(request: Request) -> dict[str, str]:
    # Electron terminates the process after this response. Flush local handles now.
    await request.app.state.container.stop()
    return {"status": "ok"}

