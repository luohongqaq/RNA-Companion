-- Hot/cold movements preserve recall visibility and do not change cached document versions.

DROP TRIGGER daily_memories_recall_update;

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

DROP TRIGGER topic_memories_recall_update;

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
