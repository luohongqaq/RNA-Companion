"""用户记忆修改命令与持久化保存输入。

UpdateDailyMemory 和 UpdateTopicMemory 携带公开正文、预期版本及幂等键，SaveMemoryEdit 携带校验后的完整内部文档。
命令只描述修改意图；版本冲突、幂等重放、正式切版和编辑记录插入由同一仓储事务完成。
"""
from __future__ import annotations
from module.memory_management.shared.core.domain.document import StructuredMemoryDocument
from module.shared.memory.types.references import MemoryKind
from dataclasses import dataclass
from module.user_edit.memory_edit.core.domain.document import DailyDocumentContent, TopicDocumentContent
from module.user_edit.memory_edit.core.domain.query import DocumentKind


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateMemory:
    character_id: str
    document_id: str
    expected_revision: int
    idempotency_key: str


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateDailyMemory(UpdateMemory):
    document: DailyDocumentContent


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateTopicMemory(UpdateMemory):
    document: TopicDocumentContent


@dataclass(frozen=True, slots=True)
class SaveMemoryEdit:
    """业务校验后的完整内部文档；仓储负责幂等、版本及原子保存。"""
    character_id: str
    kind: DocumentKind
    document_id: str
    expected_revision: int
    idempotency_key: str
    document: StructuredMemoryDocument


@dataclass(frozen=True, slots=True)
class UpdateMemoryCommand:
    """用户提交结构化修改，绑定基础版本与幂等键，创建下一revision。"""
    character_id: str
    kind: MemoryKind
    document_id: str
    base_revision: int
    base_content_hash: str
    idempotency_key: str
    document: StructuredMemoryDocument
