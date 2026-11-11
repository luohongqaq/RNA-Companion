-- Fresh database baseline at schema version 7.
-- Existing databases upgrade through numbered migrations.

CREATE TABLE attempt_memory_refs (
    attempt_id TEXT NOT NULL REFERENCES generation_attempts(attempt_id),
    memory_type TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY(attempt_id, memory_type, memory_id)
);

CREATE TABLE characters (
    character_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    character_setting TEXT NOT NULL,
    card_path TEXT,
    user_appellation TEXT NOT NULL DEFAULT ''
);

CREATE TABLE chat_events (
	event_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	event_type TEXT NOT NULL, 
	conversation_id TEXT NOT NULL, 
	job_id TEXT NOT NULL, 
	message_id TEXT, 
	reply_json JSON, 
	failure_json JSON, 
	cancel_json JSON, 
	manual_retry_count INTEGER, 
	created_at TEXT NOT NULL, 
	CHECK (event_type IN ('reply_committed', 'generation_failed', 'generation_cancelled')), 
	CHECK ((event_type = 'reply_committed' AND message_id IS NOT NULL AND reply_json IS NOT NULL AND json_valid(reply_json) AND json_type(reply_json) = 'object' AND failure_json IS NULL AND manual_retry_count IS NULL AND cancel_json IS NULL) OR (event_type = 'generation_failed' AND message_id IS NULL AND reply_json IS NULL AND failure_json IS NOT NULL AND json_valid(failure_json) AND json_type(failure_json) = 'object' AND manual_retry_count IS NOT NULL AND manual_retry_count >= 0 AND cancel_json IS NULL) OR (event_type = 'generation_cancelled' AND message_id IS NULL AND reply_json IS NULL AND failure_json IS NULL AND cancel_json IS NOT NULL AND json_valid(cancel_json) AND json_type(cancel_json) = 'object' AND manual_retry_count IS NOT NULL AND manual_retry_count >= 0)), 
	CONSTRAINT uq_chat_events_message UNIQUE (event_type, message_id), 
	CONSTRAINT uq_chat_events_failure UNIQUE (event_type, job_id, manual_retry_count), 
	FOREIGN KEY(conversation_id) REFERENCES conversations (conversation_id), 
	FOREIGN KEY(job_id) REFERENCES generation_jobs (job_id), 
	FOREIGN KEY(message_id) REFERENCES messages (message_id)
);

CREATE TABLE context_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    target_key TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE "context_snapshots" (
    snapshot_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE REFERENCES sessions(session_id),
    covered_through_turn_seq INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    rendered_summary_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    snapshot_state TEXT NOT NULL CHECK (snapshot_state IN ('ready', 'valid', 'invalidated')),
    created_at TEXT NOT NULL,
    rendered_token_count INTEGER NOT NULL DEFAULT 0 CHECK (rendered_token_count >= 0),
    source_manifest_json TEXT,
    versions_json TEXT NOT NULL DEFAULT '{"version":1}',
    producer_job_id TEXT UNIQUE
);

CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(character_id),
    current_session_id TEXT REFERENCES sessions(session_id),
    title TEXT,
    state TEXT NOT NULL CHECK (state IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE daily_memories (
    daily_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(character_id),
    memory_date DATE NOT NULL,
    source_session_ids_json JSON NOT NULL,
    document_json JSON NOT NULL,
    revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    vector_document_key TEXT NOT NULL,
    published_at TEXT NOT NULL,
    recall_tier TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, last_mentioned_at DATE,
    UNIQUE(character_id, memory_date)
);

CREATE TABLE generation_attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES generation_jobs(job_id),
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    attempt_version INTEGER NOT NULL,
    input_version INTEGER NOT NULL,
    active_token INTEGER NOT NULL,
    attempt_state TEXT NOT NULL,
    input_boundary_json TEXT NOT NULL,
    frozen_prompt_json TEXT,
    candidate_json TEXT,
    failure_json TEXT,
    context_manifest_json TEXT,
    memory_manifest_json TEXT,
    prompt_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT, memory_audit_json JSON,
    UNIQUE(turn_id, attempt_version)
);

CREATE TABLE generation_jobs (
    job_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    expected_input_version INTEGER NOT NULL,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    current_attempt_id TEXT REFERENCES generation_attempts(attempt_id),
    retry_count INTEGER NOT NULL DEFAULT 0,
    failure_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
, manual_retry_count INTEGER NOT NULL DEFAULT 0 CHECK (manual_retry_count >= 0));

CREATE TABLE memory_candidates (
    candidate_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(character_id),
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    object_name TEXT NOT NULL,
    statement TEXT NOT NULL,
    occurrence_time TEXT,
    evidence_json TEXT NOT NULL,
    forgetfulness INTEGER NOT NULL,
    candidate_state TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE memory_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    target_key TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE "memory_objects" (
    object_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(character_id),
    object_name TEXT NOT NULL,
    events_json JSON NOT NULL,
    plans_json JSON NOT NULL,
    source_position_key TEXT NOT NULL UNIQUE,
    forgetfulness INTEGER NOT NULL CHECK (forgetfulness IN (0, 1)),
    object_state TEXT NOT NULL CHECK (object_state IN ('staged', 'linked', 'discarded')),
    recall_tier TEXT NOT NULL CHECK (recall_tier IN ('hot', 'cold', 'archived')),
    linked_topic_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE memory_recall_cache (
    character_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    signal_seq INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(character_id, conversation_id)
);

CREATE TABLE memory_source_refs (
    memory_ref_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    selection_round INTEGER NOT NULL,
    memory_type TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content TEXT NOT NULL,
    selected_at TEXT NOT NULL,
    UNIQUE(turn_id, selection_round, memory_type, memory_id, revision)
);

CREATE TABLE memory_update_clock (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    seq INTEGER NOT NULL
);

CREATE TABLE memory_update_signals (
    character_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    update_seq INTEGER NOT NULL,
    PRIMARY KEY(character_id, document_id)
);

CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    message_seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'character')),
    content TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT '',
    visual_json TEXT,
    request_id TEXT UNIQUE,
    client_created_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(turn_id, message_seq)
);

CREATE TABLE request_receipts (
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    message_id TEXT REFERENCES messages(message_id),
    turn_id TEXT REFERENCES turns(turn_id),
    input_version INTEGER,
    job_id TEXT REFERENCES generation_jobs(job_id),
    receipt_state TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(request_id, operation)
);

CREATE TABLE "sessions" (
    session_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    predecessor_session_id TEXT REFERENCES sessions(session_id),
    auto_successor_of TEXT UNIQUE REFERENCES sessions(session_id),
    session_state TEXT NOT NULL CHECK (session_state IN ('open', 'waiting_handoff', 'sealed')),
    session_progress_tokens INTEGER NOT NULL DEFAULT 0,
    session_start_time TEXT NOT NULL,
    session_end_time TEXT
, inherited_context_json TEXT, daily_jobs_registered_at TEXT, topic_jobs_registered_at TEXT, session_seq INTEGER NOT NULL DEFAULT 1 CHECK (session_seq >= 1));

CREATE TABLE storage_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    parent_job_id TEXT REFERENCES memory_jobs(job_id),
    target_key TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE topic_memories (
    topic_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(character_id),
    revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    recall_tier TEXT NOT NULL,
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, document_json JSON, vector_document_key TEXT, published_at TEXT, last_mentioned_at DATE);

CREATE TABLE turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    turn_seq INTEGER NOT NULL,
    input_version INTEGER NOT NULL DEFAULT 0,
    row_version INTEGER NOT NULL DEFAULT 0,
    turn_state TEXT NOT NULL CHECK (turn_state IN ('current', 'complete')),
    accepted_message_id TEXT REFERENCES messages(message_id),
    active_attempt_id TEXT REFERENCES generation_attempts(attempt_id),
    active_token INTEGER NOT NULL DEFAULT 0,
    turn_token_count INTEGER NOT NULL DEFAULT 0,
    cumulative_token_count INTEGER NOT NULL DEFAULT 0,
    tokenizer_version TEXT, content_revision INTEGER NOT NULL DEFAULT 1 CHECK (content_revision >= 1),
    UNIQUE(session_id, turn_seq)
);

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

CREATE INDEX ix_chat_events_conversation ON chat_events(conversation_id, event_id);

CREATE INDEX ix_context_jobs_ready
ON context_jobs(status, available_at, priority, created_at);

CREATE INDEX ix_generation_jobs_ready
ON generation_jobs(state, available_at, priority, created_at);

CREATE INDEX ix_memory_jobs_ready
ON memory_jobs(status, available_at, priority, created_at);

CREATE INDEX ix_memory_objects_pending ON memory_objects(character_id, object_state, created_at, object_id);

CREATE INDEX ix_memory_update_signals_recent ON memory_update_signals(character_id, update_seq DESC);

CREATE INDEX ix_sessions_daily_registration
ON sessions(session_state, daily_jobs_registered_at, session_end_time, session_id);

CREATE INDEX ix_sessions_topic_registration
ON sessions(session_state, topic_jobs_registered_at, session_end_time, session_id);

CREATE INDEX ix_storage_jobs_ready
ON storage_jobs(status, available_at, priority, created_at);

CREATE INDEX ix_topic_memories_pool
ON topic_memories(character_id, recall_tier, updated_at, topic_id);

CREATE UNIQUE INDEX uq_sessions_conversation_seq ON sessions(conversation_id, session_seq);

CREATE UNIQUE INDEX uq_sessions_one_open
ON sessions(conversation_id) WHERE session_state IN ('open', 'waiting_handoff');

CREATE UNIQUE INDEX uq_turns_one_current
ON turns(session_id) WHERE turn_state = 'current';

CREATE INDEX ix_memory_edits_ready
ON memory_edits(index_status, available_at, created_at, edit_id);

CREATE INDEX ix_memory_edits_document
ON memory_edits(character_id, kind, document_id, revision);

CREATE TRIGGER daily_memories_recall_delete AFTER DELETE ON daily_memories
BEGIN
    UPDATE memory_update_clock SET seq = seq + 1;
    INSERT INTO memory_update_signals(character_id, document_id, kind, revision, content_hash, deleted, update_seq)
    VALUES (OLD.character_id, OLD.daily_id, 'daily', OLD.revision, OLD.content_hash, 1,
            (SELECT seq FROM memory_update_clock))
    ON CONFLICT(character_id, document_id) DO UPDATE SET kind=excluded.kind, revision=excluded.revision,
        content_hash=excluded.content_hash, deleted=excluded.deleted, update_seq=excluded.update_seq;
END;

CREATE TRIGGER daily_memories_recall_insert AFTER INSERT ON daily_memories
BEGIN
    UPDATE memory_update_clock SET seq = seq + 1;
    INSERT INTO memory_update_signals(character_id, document_id, kind, revision, content_hash, deleted, update_seq)
    VALUES (NEW.character_id, NEW.daily_id, 'daily', NEW.revision, NEW.content_hash,
        CASE WHEN NEW.recall_tier NOT IN ('hot', 'cold') OR NEW.published_at IS NULL
             OR NEW.document_json IS NULL OR NEW.vector_document_key IS NULL THEN 1 ELSE 0 END,
        (SELECT seq FROM memory_update_clock))
    ON CONFLICT(character_id, document_id) DO UPDATE SET kind=excluded.kind, revision=excluded.revision,
        content_hash=excluded.content_hash, deleted=excluded.deleted, update_seq=excluded.update_seq;
END;

CREATE TRIGGER topic_memories_recall_delete AFTER DELETE ON topic_memories
BEGIN
    UPDATE memory_update_clock SET seq = seq + 1;
    INSERT INTO memory_update_signals(character_id, document_id, kind, revision, content_hash, deleted, update_seq)
    VALUES (OLD.character_id, OLD.topic_id, 'topic', OLD.revision, OLD.content_hash, 1,
            (SELECT seq FROM memory_update_clock))
    ON CONFLICT(character_id, document_id) DO UPDATE SET kind=excluded.kind, revision=excluded.revision,
        content_hash=excluded.content_hash, deleted=excluded.deleted, update_seq=excluded.update_seq;
END;

CREATE TRIGGER topic_memories_recall_insert AFTER INSERT ON topic_memories
BEGIN
    UPDATE memory_update_clock SET seq = seq + 1;
    INSERT INTO memory_update_signals(character_id, document_id, kind, revision, content_hash, deleted, update_seq)
    VALUES (NEW.character_id, NEW.topic_id, 'topic', NEW.revision, NEW.content_hash,
        CASE WHEN NEW.recall_tier NOT IN ('hot', 'cold') OR NEW.published_at IS NULL
             OR NEW.document_json IS NULL OR NEW.vector_document_key IS NULL THEN 1 ELSE 0 END,
        (SELECT seq FROM memory_update_clock))
    ON CONFLICT(character_id, document_id) DO UPDATE SET kind=excluded.kind, revision=excluded.revision,
        content_hash=excluded.content_hash, deleted=excluded.deleted, update_seq=excluded.update_seq;
END;

CREATE TRIGGER daily_memories_recall_update AFTER UPDATE ON daily_memories
WHEN OLD.revision IS NOT NEW.revision OR OLD.content_hash IS NOT NEW.content_hash
     OR OLD.vector_document_key IS NOT NEW.vector_document_key
     OR (OLD.recall_tier IN ('hot', 'cold')) IS NOT (NEW.recall_tier IN ('hot', 'cold'))
     OR (OLD.published_at IS NULL) != (NEW.published_at IS NULL)
     OR OLD.document_json IS NOT NEW.document_json
BEGIN
    UPDATE memory_update_clock SET seq = seq + 1;
    INSERT INTO memory_update_signals(character_id, document_id, kind, revision, content_hash, deleted, update_seq)
    VALUES (NEW.character_id, NEW.daily_id, 'daily', NEW.revision, NEW.content_hash,
        CASE WHEN NEW.recall_tier NOT IN ('hot', 'cold') OR NEW.published_at IS NULL
             OR NEW.document_json IS NULL OR NEW.vector_document_key IS NULL THEN 1 ELSE 0 END,
        (SELECT seq FROM memory_update_clock))
    ON CONFLICT(character_id, document_id) DO UPDATE SET kind=excluded.kind, revision=excluded.revision,
        content_hash=excluded.content_hash, deleted=excluded.deleted, update_seq=excluded.update_seq;
END;

CREATE TRIGGER topic_memories_recall_update AFTER UPDATE ON topic_memories
WHEN OLD.revision IS NOT NEW.revision OR OLD.content_hash IS NOT NEW.content_hash
     OR OLD.vector_document_key IS NOT NEW.vector_document_key
     OR (OLD.recall_tier IN ('hot', 'cold')) IS NOT (NEW.recall_tier IN ('hot', 'cold'))
     OR (OLD.published_at IS NULL) != (NEW.published_at IS NULL)
     OR OLD.document_json IS NOT NEW.document_json
BEGIN
    UPDATE memory_update_clock SET seq = seq + 1;
    INSERT INTO memory_update_signals(character_id, document_id, kind, revision, content_hash, deleted, update_seq)
    VALUES (NEW.character_id, NEW.topic_id, 'topic', NEW.revision, NEW.content_hash,
        CASE WHEN NEW.recall_tier NOT IN ('hot', 'cold') OR NEW.published_at IS NULL
             OR NEW.document_json IS NULL OR NEW.vector_document_key IS NULL THEN 1 ELSE 0 END,
        (SELECT seq FROM memory_update_clock))
    ON CONFLICT(character_id, document_id) DO UPDATE SET kind=excluded.kind, revision=excluded.revision,
        content_hash=excluded.content_hash, deleted=excluded.deleted, update_seq=excluded.update_seq;
END;

INSERT INTO memory_update_clock(singleton, seq) VALUES (1, 0);

INSERT INTO schema_migrations(version) VALUES (1), (2), (3), (4), (5), (6), (7);
