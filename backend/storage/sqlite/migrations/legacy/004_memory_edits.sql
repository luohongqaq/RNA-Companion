-- 用户编辑直接保存正式正文；本表保留每次编辑快照及待处理的索引状态。
-- kind/document_id 为跨 daily/topic 的逻辑引用，归属及版本检查由写入仓储完成。
CREATE TABLE memory_edits (
    edit_id TEXT PRIMARY KEY NOT NULL,
    character_id TEXT NOT NULL REFERENCES characters(character_id),
    kind TEXT NOT NULL CHECK (kind IN ('daily', 'topic')),
    document_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    base_content_hash TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    document_json JSON NOT NULL CHECK (json_type(document_json) = 'object'),
    index_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (index_status IN ('pending', 'indexing', 'done', 'failed', 'obsolete')),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    attempt_version INTEGER NOT NULL DEFAULT 0 CHECK (attempt_version >= 0),
    available_at TEXT NOT NULL,
    last_error TEXT,
    indexed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (base_revision >= 1 AND revision = base_revision + 1),
    CHECK ((index_status = 'done' AND indexed_at IS NOT NULL) OR
           (index_status != 'done' AND indexed_at IS NULL)),
    CONSTRAINT uq_memory_edits_request UNIQUE (character_id, idempotency_key),
    CONSTRAINT uq_memory_edits_revision UNIQUE (character_id, kind, document_id, revision)
);

CREATE INDEX ix_memory_edits_ready
ON memory_edits(index_status, available_at, created_at, edit_id);

CREATE INDEX ix_memory_edits_document
ON memory_edits(character_id, kind, document_id, revision);
