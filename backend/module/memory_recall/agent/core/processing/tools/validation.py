"""工具参数的 JSON Schema 结构校验。

职责：按 contracts.schemas 声明的 Schema（与发给模型的是同一份契约）做结构层
校验——类型、必填、枚举、长度、数量、去重、禁多余字段、拒空白串、
拒 bool 冒充 integer，不做宽松类型转换。跨字段语义在 contracts.models 的
输入模型，准入约束在 registry.py 的 admission_checks。
"""
from __future__ import annotations
import json
from module.memory_recall.agent.core.processing.tools.schemas import TOOLS
from module.memory_recall.shared.core.domain.errors import (
    BLANK_VALUE,
    DUPLICATE_ITEM,
    EXTRA_FIELD,
    INVALID_ENUM,
    INVALID_LENGTH,
    INVALID_TYPE,
    MISSING_FIELD,
    OUT_OF_RANGE,
)
from typing import Any


SCHEMAS = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}


class RecallToolInputValidator:
    @staticmethod
    def schema_errors(value: Any, schema: dict[str, Any], path: str = "arguments") -> list[dict]:
        """递归检查本项目工具 Schema 使用的结构约束。

        功能：检查对象字段、必填项、类型、枚举、长度、数量、去重及数值范围；
        拒绝纯空白字符串和 bool 冒充 integer，不进行宽松类型转换。
        输入：value 为待校验值，schema 为对应 JSON Schema 片段，path 为错误路径。
        输出：错误条目列表；空列表表示结构约束通过。只覆盖本项目使用的 Schema
        关键字，跨字段和准入约束由输入模型与 admission_checks 补充。
        """
        expected = schema.get("type")
        valid = {
            "object": isinstance(value, dict), "array": isinstance(value, list),
            "string": isinstance(value, str), "integer": type(value) is int,
        }.get(expected, True)
        if not valid:
            return [INVALID_TYPE.build(f"{path} 必须是 {expected}")]
        errors = []
        if expected == "object":
            props = schema.get("properties", {})
            for key in schema.get("required", []):
                if key not in value:
                    errors.append(MISSING_FIELD.build(f"缺少 {path}.{key}"))
            for key, child in value.items():
                if key in props:
                    errors.extend(RecallToolInputValidator.schema_errors(child, props[key], f"{path}.{key}"))
                elif schema.get("additionalProperties") is False:
                    errors.append(EXTRA_FIELD.build(f"不允许字段 {path}.{key}"))
        elif expected in ("array", "string"):
            low, high = ("minItems", "maxItems") if expected == "array" else ("minLength", "maxLength")
            if not schema.get(low, 0) <= len(value) <= schema.get(high, float("inf")):
                errors.append(INVALID_LENGTH.build(f"{path} 长度超出允许范围"))
            if expected == "string" and not value.strip():
                errors.append(BLANK_VALUE.build(f"{path} 不能为空白"))
            if expected == "array":
                if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
                    errors.append(DUPLICATE_ITEM.build(f"{path} 不允许重复项"))
                for index, child in enumerate(value):
                    errors.extend(RecallToolInputValidator.schema_errors(child, schema.get("items", {}), f"{path}[{index}]"))
        elif expected == "integer":
            if not schema.get("minimum", -float("inf")) <= value <= schema.get("maximum", float("inf")):
                errors.append(OUT_OF_RANGE.build(f"{path} 超出允许范围"))
        if "enum" in schema and value not in schema["enum"]:
            errors.append(INVALID_ENUM.build(f"{path} 必须属于 {schema['enum']}"))
        return errors
