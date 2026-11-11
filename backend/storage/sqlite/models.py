"""sqlAlchemy ORM类，基本一一对应"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Date, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from storage.sqlite.types import UTCDateTime


class Base(DeclarativeBase):
    pass


class CharacterRow(Base):
    """角色表，对应Character"""
    __tablename__ = "characters"

    character_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    character_setting: Mapped[str] = mapped_column(Text)
    card_path: Mapped[str | None] = mapped_column(Text) #角色提示词目录，包含角色卡/系统/语气文件
    user_appellation: Mapped[str] = mapped_column(Text, nullable=False, server_default="") #该角色对用户的称呼，渲染角色卡 {{ user_name }}


class ConversationRow(Base):
    """对话表，对应Conversation"""
    __tablename__ = "conversations"
    __table_args__ = (CheckConstraint("state IN ('active', 'archived')"),)

    conversation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.character_id"))
    current_session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"))
    title: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class SessionRow(Base):
    """会话表，对应Session"""
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("conversation_id", "session_seq", name="uq_sessions_conversation_seq"),
        CheckConstraint("session_seq >= 1"),
        CheckConstraint("session_state IN ('open', 'waiting_handoff', 'sealed')"),
        Index("uq_sessions_one_open", "conversation_id", unique=True,
              sqlite_where=text("session_state IN ('open', 'waiting_handoff')")),
        Index("ix_sessions_daily_registration", "session_state", "daily_jobs_registered_at",
              "session_end_time", "session_id"),
        Index("ix_sessions_topic_registration", "session_state", "topic_jobs_registered_at",
              "session_end_time", "session_id"),
    )

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id"))
    predecessor_session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"))
    session_seq: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    auto_successor_of: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), unique=True)
    inherited_context_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    session_state: Mapped[str] = mapped_column(Text)
    session_progress_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    session_start_time: Mapped[datetime] = mapped_column(UTCDateTime())
    session_end_time: Mapped[datetime | None] = mapped_column(UTCDateTime())
    daily_jobs_registered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    topic_jobs_registered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class TurnRow(Base):
    """轮次表，对应Turn"""
    __tablename__ = "turns"
    __table_args__ = (
        UniqueConstraint("session_id", "turn_seq"),
        CheckConstraint("turn_state IN ('current', 'complete')"),
        Index("uq_turns_one_current", "session_id", unique=True,
              sqlite_where=text("turn_state = 'current'")),
    )

    turn_id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"))
    turn_seq: Mapped[int] = mapped_column(Integer)
    input_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    row_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0") #乐观锁：一个条件就表达“自我读它以来，这行没有任何变化”。
    content_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    turn_state: Mapped[str] = mapped_column(Text)
    accepted_message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.message_id"))
    active_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("generation_attempts.attempt_id"))
    active_token: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    turn_token_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cumulative_token_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokenizer_version: Mapped[str | None] = mapped_column(Text)


class MessageRow(Base):
    """消息表，对应Message"""
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("turn_id", "message_seq"),
        CheckConstraint("role IN ('user', 'character')"),
    )

    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("turns.turn_id"))
    message_seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, default="", server_default="")
    visual_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    request_id: Mapped[str | None] = mapped_column(Text, unique=True)
    client_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ChatEventRow(Base):
    """已提交的聊天事件；与回复、最终失败或主动取消在同一事务内登记。"""
    __tablename__ = "chat_events"
    __table_args__ = (
        CheckConstraint("event_type IN ('reply_committed', 'generation_failed', 'generation_cancelled')"),
        CheckConstraint("(event_type = 'reply_committed' AND message_id IS NOT NULL "
                        "AND reply_json IS NOT NULL AND json_valid(reply_json) "
                        "AND json_type(reply_json) = 'object' AND failure_json IS NULL "
                        "AND manual_retry_count IS NULL AND cancel_json IS NULL) OR "
                        "(event_type = 'generation_failed' AND message_id IS NULL AND reply_json IS NULL "
                        "AND failure_json IS NOT NULL AND json_valid(failure_json) "
                        "AND json_type(failure_json) = 'object' AND manual_retry_count IS NOT NULL "
                        "AND manual_retry_count >= 0 AND cancel_json IS NULL) OR "
                        "(event_type = 'generation_cancelled' AND message_id IS NULL AND reply_json IS NULL "
                        "AND failure_json IS NULL AND cancel_json IS NOT NULL AND json_valid(cancel_json) "
                        "AND json_type(cancel_json) = 'object' AND manual_retry_count IS NOT NULL "
                        "AND manual_retry_count >= 0)"),
        UniqueConstraint("event_type", "message_id", name="uq_chat_events_message"),
        UniqueConstraint("event_type", "job_id", "manual_retry_count", name="uq_chat_events_failure"),
        Index("ix_chat_events_conversation", "conversation_id", "event_id"),
        {"sqlite_autoincrement": True},
    )

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(Text)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.job_id"))
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.message_id"))
    reply_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    failure_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    cancel_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    manual_retry_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class GenerationJobRow(Base):
    """生成任务表，对应GenerationJob"""
    __tablename__ = "generation_jobs"
    __table_args__ = (Index("ix_generation_jobs_ready", "state", "available_at", "priority", "created_at"),)

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("turns.turn_id"))
    expected_input_version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(UTCDateTime())
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    current_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("generation_attempts.attempt_id"))
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    manual_retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failure_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class GenerationAttemptRow(Base):
    """生成任务尝试表，对应GenerationAttempt"""
    __tablename__ = "generation_attempts"
    __table_args__ = (UniqueConstraint("turn_id", "attempt_version"),)

    attempt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.job_id"))
    turn_id: Mapped[str] = mapped_column(ForeignKey("turns.turn_id"))
    attempt_version: Mapped[int] = mapped_column(Integer)
    input_version: Mapped[int] = mapped_column(Integer)
    active_token: Mapped[int] = mapped_column(Integer)
    attempt_state: Mapped[str] = mapped_column(Text)
    input_boundary_json: Mapped[dict[str, Any]] = mapped_column(JSON(none_as_null=True))
    frozen_prompt_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    candidate_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    failure_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    context_manifest_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    memory_manifest_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    memory_audit_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    prompt_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RequestReceiptRow(Base):
    """请求收据表，对应RequestReceipt"""
    __tablename__ = "request_receipts"

    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    operation: Mapped[str] = mapped_column(Text, primary_key=True)
    payload_hash: Mapped[str] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.message_id"))
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("turns.turn_id"))
    input_version: Mapped[int | None] = mapped_column(Integer)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.job_id"))
    receipt_state: Mapped[str] = mapped_column(Text)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON) #result_json 的角色是“当时响应的原文快照”, 完整的SaveMessageResult
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ContextSnapshotRow(Base):
    """每个session唯一的交接摘要，对应ContextSnapshot。"""
    __tablename__ = "context_snapshots"
    __table_args__ = (
        CheckConstraint("snapshot_state IN ('ready', 'valid', 'invalidated')"),
        UniqueConstraint("session_id"),
    )

    snapshot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"))
    covered_through_turn_seq: Mapped[int] = mapped_column(Integer)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    rendered_summary_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    snapshot_state: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    rendered_token_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source_manifest_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    versions_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=lambda: {"version": 1}, server_default='{"version":1}')
    producer_job_id: Mapped[str | None] = mapped_column(Text, unique=True)


class ContextJobRow(Base):
    """上下文任务；阶段产物保存在带 schema_version 的 payload_json 中。"""
    __tablename__ = "context_jobs"
    __table_args__ = (Index("ix_context_jobs_ready", "status", "available_at", "priority", "created_at"),)

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_type: Mapped[str] = mapped_column(Text)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"))
    target_key: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    priority: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class MemoryJobRow(Base):
    """长期记忆生产任务；输入和阶段产物保存在带版本号的payload_json中。"""
    __tablename__ = "memory_jobs"
    __table_args__ = (Index("ix_memory_jobs_ready", "status", "available_at", "priority", "created_at"),)

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_type: Mapped[str] = mapped_column(Text)
    target_key: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    priority: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class DailyMemoryRow(Base):
    """每角色每天唯一的正式daily，版本与结构化正文一起保存。"""
    __tablename__ = "daily_memories"
    __table_args__ = (
        UniqueConstraint("character_id", "memory_date"),
    )

    daily_id: Mapped[str] = mapped_column(Text, primary_key=True)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.character_id"))
    memory_date: Mapped[date] = mapped_column(Date)
    source_session_ids_json: Mapped[list[str]] = mapped_column(JSON)
    document_json: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(Text)
    vector_document_key: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime())
    recall_tier: Mapped[str] = mapped_column(Text)
    last_mentioned_at: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class TopicMemoryRow(Base):
    """SQLite当前正式topic版本；生成进度和待发布正文仍保存在任务中。"""
    __tablename__ = "topic_memories"
    __table_args__ = (
        Index("ix_topic_memories_pool", "character_id", "recall_tier", "updated_at", "topic_id"),
    )

    topic_id: Mapped[str] = mapped_column(Text, primary_key=True)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.character_id"))
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(Text)
    recall_tier: Mapped[str] = mapped_column(Text)
    source_refs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, server_default="[]")
    # 旧表只登记元数据，迁移时保留NULL；正式读取禁止把缺正文记录当作完整topic。
    document_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    vector_document_key: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_mentioned_at: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class MemoryEditRow(Base):
    """用户已保存的编辑快照；索引状态独立推进，不回滚用户正文。

    正式文档切版与本记录插入必须由仓储在同一个事务完成。
    kind + document_id 指向 daily/topic，跨两张表的归属与版本由仓储检查。
    """
    __tablename__ = "memory_edits"
    __table_args__ = (
        CheckConstraint("kind IN ('daily', 'topic')"),
        CheckConstraint("base_revision >= 1 AND revision = base_revision + 1"),
        CheckConstraint("json_type(document_json) = 'object'"),
        CheckConstraint("index_status IN ('pending', 'indexing', 'done', 'failed', 'obsolete')"),
        CheckConstraint("retry_count >= 0 AND attempt_version >= 0"),
        CheckConstraint("(index_status = 'done' AND indexed_at IS NOT NULL) OR "
                        "(index_status != 'done' AND indexed_at IS NULL)"),
        UniqueConstraint("character_id", "idempotency_key", name="uq_memory_edits_request"),
        UniqueConstraint("character_id", "kind", "document_id", "revision", name="uq_memory_edits_revision"),
        Index("ix_memory_edits_ready", "index_status", "available_at", "created_at", "edit_id"),
        Index("ix_memory_edits_document", "character_id", "kind", "document_id", "revision"),
    )

    edit_id: Mapped[str] = mapped_column(Text, primary_key=True)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.character_id"))
    kind: Mapped[str] = mapped_column(Text)
    document_id: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)  # 网络重发同一次编辑时复用
    base_revision: Mapped[int] = mapped_column(Integer)
    base_content_hash: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)  # 此次保存产生的新版本
    content_hash: Mapped[str] = mapped_column(Text)
    document_json: Mapped[dict[str, Any]] = mapped_column(JSON)  # 固定快照，重试不读取后续修改
    index_status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    attempt_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 防止迟到执行覆盖新状态
    available_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    indexed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class MemoryObjectRow(Base):
    """已激活的正式object；staged表示等待topic整合，不是未发布。"""
    __tablename__ = "memory_objects"
    __table_args__ = (
        UniqueConstraint("source_position_key"),
        CheckConstraint("forgetfulness IN (0, 1)"),
        CheckConstraint("object_state IN ('staged', 'linked', 'discarded')"),
        CheckConstraint("recall_tier IN ('hot', 'cold', 'archived')"),
        Index("ix_memory_objects_pending", "character_id", "object_state", "created_at", "object_id"),
    )

    object_id: Mapped[str] = mapped_column(Text, primary_key=True)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.character_id"))
    object_name: Mapped[str] = mapped_column(Text)
    events_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    plans_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    source_position_key: Mapped[str] = mapped_column(Text)  #object 候选的身份/幂等键——按“它出现在模型输出的第几个位置”生成
    forgetfulness: Mapped[int] = mapped_column(Integer)
    object_state: Mapped[str] = mapped_column(Text)
    recall_tier: Mapped[str] = mapped_column(Text)
    linked_topic_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
