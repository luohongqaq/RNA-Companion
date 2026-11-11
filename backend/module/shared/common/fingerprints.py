"""Stable SHA-256 hashes for canonical JSON payloads."""
import hashlib
import json
from typing import Any


class CanonicalJsonFingerprint:
    @staticmethod
    def sha256(payload: Any) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
