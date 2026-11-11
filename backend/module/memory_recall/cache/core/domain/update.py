"""正式记忆变化通知的数据契约。

MemoryUpdate 记录文档身份、类别、修订版本、内容哈希和删除标记。
缓存刷新依据这些信号核对已有材料，不把向量检索命中当作正式更新依据。
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MemoryUpdate:
    document_id: str
    kind: str
    revision: int
    content_hash: str
    deleted: bool = False
