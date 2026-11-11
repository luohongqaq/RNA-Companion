"""用户记忆查询游标的编解码与作用域检查。

用版本化游标绑定角色、记忆类别、翻页方向和稳定位置，分别支持 Daily 向后和 Topic 向前分页。
游标不跨角色或类别复用。
"""
from __future__ import annotations
import base64
import binascii
import json
from module.shared.common.errors.base import InvalidInputError
from module.shared.memory.types.references import MemoryKind
from module.user_edit.memory_edit.core.domain.query import DocumentKind


class MemoryCursorCodec:
    @staticmethod
    def decode(cursor: str | None, character_id: str, kind: DocumentKind) -> dict[str, str] | None:
        """统一解码与归属检查，两类列表使用各自的排序字段和翻页方向。"""
        if cursor is None:
            return None
        direction, time_field = ("after", "memory_date") if kind == MemoryKind.DAILY else ("before", "updated_at")
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
                raise ValueError
            data = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
            if (not isinstance(data, dict) or type(data.get("v")) is not int or data["v"] != 2
                    or data.get("character_id") != character_id or data.get("kind") != kind):
                raise ValueError
            key = data[direction]
            if (not isinstance(key, dict) or set(key) != {time_field, "document_id"}
                    or not isinstance(key[time_field], str) or not isinstance(key["document_id"], str)
                    or not 1 <= len(key["document_id"]) <= 128):
                raise ValueError
            return key
        except (ValueError, TypeError, KeyError, binascii.Error) as exc:
            raise InvalidInputError("记忆游标无效或不属于当前角色及文档类型") from exc

    @staticmethod
    def encode(character_id: str, kind: DocumentKind, key: dict[str, str]) -> str:
        direction = "after" if kind == MemoryKind.DAILY else "before"
        raw = json.dumps({"v": 2, "character_id": character_id, "kind": kind, direction: key},
                         separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
