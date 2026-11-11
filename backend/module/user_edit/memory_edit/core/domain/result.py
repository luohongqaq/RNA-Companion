"""用户记忆查询、保存及索引状态的返回结果。

列表与详情只投影所属角色的正式记忆和公开正文；保存结果返回新版本及索引状态。
编辑详情提供重试次数、可用时间、错误和完成时间，不暴露完整内部快照、聊天证据或执行代次。
"""
from __future__ import annotations
from module.memory_management.shared.core.domain.document import DailyMemory, TopicMemory
from module.shared.memory.types.references import MemoryKind
from dataclasses import dataclass
from datetime import date, datetime
from module.user_edit.memory_edit.core.domain.document import DailyDocumentContent, TopicDocumentContent
from module.user_edit.memory_edit.core.domain.job import MemoryEditStatus
from module.user_edit.memory_edit.core.domain.query import DocumentKind


@dataclass(frozen=True, slots=True)
class MemoryListItem:
    """列表摘要；标题来自当前正式正文。"""
    document_id: str  # 与 kind 一起确定文档身份
    kind: DocumentKind
    title: str
    revision: int  # 当前正式版本号；后续编辑用于防止覆盖较新版本
    updated_at: datetime  # 正式文档更新时间，不是生成任务的运行时间


@dataclass(frozen=True, slots=True)
class DailyMemoryListItem(MemoryListItem):
    """日记列表额外展示归属日期；按该日期排序，不受后续编辑时间影响。"""
    memory_date: date


@dataclass(frozen=True, slots=True)
class MemoryDetails:
    """查询详情结果；只承载当前正式版本，不包含索引或任务状态。"""
    document_id: str  # 与 kind 一起确定文档身份
    character_id: str
    kind: DocumentKind
    revision: int  # 当前正式版本号；后续编辑用于防止覆盖较新版本
    updated_at: datetime  # 正式文档更新时间，不是生成任务的运行时间
    document: DailyDocumentContent | TopicDocumentContent

    @classmethod
    def from_memory(cls, memory: DailyMemory | TopicMemory, *, character_id: str) -> MemoryDetails:
        """投影正式版本；角色归属须由查询仓储核对，不能从正文推断。"""
        if isinstance(memory, DailyMemory) and memory.kind == MemoryKind.DAILY:
            document = DailyDocumentContent.from_document(memory.document)
        elif isinstance(memory, TopicMemory) and memory.kind == MemoryKind.TOPIC:
            document = TopicDocumentContent.from_document(memory.document)
        else:
            raise ValueError("记忆详情只支持类型一致的daily或topic正式文档")
        return cls(memory.document_id, character_id, memory.kind, memory.current_revision,
                   memory.updated_at, document)


@dataclass(frozen=True, slots=True)
class MemoryEditResult:
    edit_id: str
    document_id: str
    kind: DocumentKind
    revision: int
    index_status: MemoryEditStatus


@dataclass(frozen=True, slots=True)
class MemoryEditDetails(MemoryEditResult):
    """编辑索引的可查询状态；不返回完整快照、聊天来源和内部执行代次。"""
    character_id: str
    retry_count: int  # 当前一轮自动重试次数；手动重试后从0开始
    available_at: datetime
    error: str | None
    indexed_at: datetime | None
    created_at: datetime
    updated_at: datetime
