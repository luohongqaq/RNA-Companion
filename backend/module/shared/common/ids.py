"""Stable identifier generation."""

from uuid import uuid4


class IdGenerator:
    @staticmethod
    def new(prefix: str) -> str:
        return f"{prefix}_{uuid4().hex}"
