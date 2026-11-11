"""记忆管理审核通用枚举。

ReviewVerdict 统一通过、修订通过和拒绝的结论，ReviewSeverity 统一审核问题的严重程度。
Daily 与 Topic 共享这些取值，具体问题字段、审核绑定和修订正文仍由各链的 review 定义。
"""
from __future__ import annotations
from enum import StrEnum


class ReviewVerdict(StrEnum):
    """长期记忆候选审核结果"""
    PASS = "pass"
    REVISED = "revised"
    REJECT = "reject"


class ReviewSeverity(StrEnum):
    """审核严重程度"""
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
