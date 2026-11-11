"""Daily 进入审核前的生成候选。

DailyCandidate 保存候选正文、候选版本、内容哈希和生成时间，供审核绑定与任务恢复使用。
本文件表达生成结果；模型请求由适配器执行，审核结论和待发布正文分别归 review 与 publication。
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from module.memory_management.daily.core.domain.document import DailyDocument


@dataclass(frozen=True, slots=True)
class DailyCandidate:
    """待审核的daily记忆文档"""
    candidate_version: int
    document: DailyDocument
    content_hash: str  # 结构化候选hash
    generated_at: datetime
