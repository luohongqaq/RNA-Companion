"""记忆查询和用户编辑的 HTTP 契约；正文复用业务层的公开类型。"""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from module.user_edit.memory_edit.core.domain.document import DailyDocumentContent, TopicDocumentContent
from module.user_edit.memory_edit.core.domain.query import DocumentKind
from module.shared.memory.types.references import MemoryKind
from module.user_edit.memory_edit.core.domain.job import MemoryEditStatus


class _MemoryResponse(BaseModel):
    """允许 model_validate() 直接读取业务 dataclass 的属性。"""
    model_config = ConfigDict(from_attributes=True)


class _UpdateMemoryRequest(BaseModel):
    """完整替换正文的公共参数；文档ID由路径提供，不接受内部证据字段。"""
    # 不启用str_strip_whitespace，避免修改用户正文的空白字符。
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(min_length=1, max_length=128, pattern=r"\S", description="文档所属角色ID")
    expected_revision: int = Field(strict=True, ge=1, description="读取时的正式版本号，用于防止覆盖并发修改")
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"\S",
        description="一次保存的唯一键；网络重发复用此键，下一次修改使用新键")


class UpdateDailyMemoryRequest(_UpdateMemoryRequest):
    """替换已有日记正文；日期必须保持原值，五个内容分区允许清空。"""
    document: DailyDocumentContent = Field(description="日记完整公开正文；不提交session、turn或证据原话")


class UpdateTopicMemoryRequest(_UpdateMemoryRequest):
    """替换已有主题正文；时间线可清空，条目引用关系由业务层进一步校验。"""
    document: TopicDocumentContent = Field(description="主题完整公开正文；不提交聊天来源")


class MemoryEditResponse(_MemoryResponse):
    """正文已保存的结果；index_status只代表索引进度，不影响正文保存是否成功。"""
    edit_id: str = Field(description="本次编辑记录ID，重复提交同一请求返回原ID")
    document_id: str = Field(description="已修改的文档ID")
    kind: DocumentKind
    revision: int = Field(ge=1, description="本次保存产生的正式版本号")
    index_status: MemoryEditStatus = Field(description="索引状态：pending/indexing/done/failed/obsolete")


class MemoryEditDetailsResponse(MemoryEditResponse):
    """查询、手动重试共用的索引状态响应；不包含内部正文快照。"""
    character_id: str
    retry_count: int = Field(ge=0, description="本轮自动重试次数，手动重试后重置为0")
    available_at: datetime = Field(description="允许后台下一次执行的时间")
    error: str | None = Field(description="最近一次索引错误；重新排队或成功后清空")
    indexed_at: datetime | None = Field(description="当前编辑版本索引完成时间")
    created_at: datetime
    updated_at: datetime


class MemoryListItemResponse(_MemoryResponse):
    """列表中的一篇正式记忆摘要，不包含完整正文。"""
    document_id: str = Field(description="文档ID，与kind一起定位文档")
    kind: DocumentKind = Field(description="正式文档类型：daily或topic")
    title: str = Field(description="当前正式正文的标题")
    revision: int = Field(ge=1, description="当前正式版本号")
    updated_at: datetime = Field(description="当前正式版本的更新时间，包含时区")


class DailyMemoryListItemResponse(MemoryListItemResponse):
    """日记列表按memory_date升序，updated_at只用于展示最后修改时间。"""
    kind: Literal[MemoryKind.DAILY]
    memory_date: date = Field(description="日记归属日期，也是列表排序日期")


class TopicMemoryListItemResponse(MemoryListItemResponse):
    """主题列表按updated_at倒序。"""
    kind: Literal[MemoryKind.TOPIC]



class MemoryDetailsResponse(_MemoryResponse):
    """一篇正式记忆及其版本；document 已由业务层过滤内部证据。"""
    document_id: str = Field(description="文档ID，与kind一起定位文档")
    character_id: str = Field(description="文档所属角色ID")
    kind: DocumentKind
    revision: int = Field(ge=1, description="当前正式版本号，后续编辑时用于检测版本冲突")
    updated_at: datetime
    document: DailyDocumentContent | TopicDocumentContent

    @model_validator(mode="after")
    def validate_document_kind(self) -> "MemoryDetailsResponse":
        """union 只验证正文形状；这里额外保证外层 kind 与正文类型一致。"""
        expected = DailyDocumentContent if self.kind == MemoryKind.DAILY else TopicDocumentContent
        if not isinstance(self.document, expected):
            raise ValueError("kind与记忆正文类型不一致")
        return self
