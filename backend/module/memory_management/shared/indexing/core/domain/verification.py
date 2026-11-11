"""索引写入核验结果。

IndexVerification 记录是否通过核验，以及缺失或格式不符的 point 标识。
供索引任务保存核验产物并判断完成条件，实际向量库读取和校验操作由实现层负责。
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IndexVerification:
    passed: bool
    missing_points: tuple[str, ...]
    invalid_points: tuple[str, ...]
