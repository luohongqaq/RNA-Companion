"""RNA Companion backend entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parent
os.chdir(BACKEND_ROOT)

for stream in (sys.stdout, sys.stderr):
    if getattr(stream, "encoding", "").lower() != "utf-8":
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


def main() -> None:
    import uvicorn

    uvicorn.run(
        "api.app:app",
        host="127.0.0.1",
        port=8765,
        log_level="info",
    )


if __name__ == "__main__":
    main()

