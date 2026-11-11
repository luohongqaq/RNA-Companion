"""Serialize recall material consistently and count it via an injected tokenizer."""
from __future__ import annotations
import json
from typing import Any, Protocol


class MaterialTokenCounter(Protocol):
    def count_text(self, text: str) -> int: ...


class RecallMaterialBudget:
    @staticmethod
    def count_tokens(value: Any, counter: MaterialTokenCounter) -> int:
        if counter is None:
            raise ValueError("记忆召回需要已配置的 token 计数器")
        return counter.count_text(json.dumps(value, ensure_ascii=False, default=str))
