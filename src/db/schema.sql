-- Lobster messages.db schema
-- BIS-159 Slice 1: messages, bisque_events, agent_events tables + FTS5
--
-- Design principles:
--   - messages:      all inbound user messages (telegram, bisque, self-check, etc.)
--                    and all outbound sent replies
--   - bisque_events: bisque-channel-specific messages (superset of bisque rows in messages)
--   - agent_events:  subagent lifecycle events (results, failures, notifications)
--   - FTS5 virtual tables index the `text` column of each table for keyword search
--   - Triggers keep FTS indices in sync with the base tables
--   - WAL mode, foreign_keys ON — set at connection time, not stored in schema

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ─── messages ────────────────────────────────────────────────────────────────
-- One row per processed inbound message or sent outbound reply.
-- Inbound rows come from ~/messages/processed/*.json.
-- Outbound rows come from ~/messages/sent/*.json.
CREATE TABLE IF NOT EXISTS messages (
    -- Identity
    id                  TEXT PRIMARY KEY,          -- original file-based id, e.g. "1769219731361_22"
    direction           TEXT NOT NULL DEFAULT 'in', -- 'in' | 'out'
    source              TEXT NOT NULL,             -- 'telegram' | 'system' | 'internal' | ...
    type                TEXT,                      -- 'telegram' | 'text' | 'voice' | 'photo' | 'self_check' | ...

    -- Participants
    chat_id             TEXT,                      -- telegram chat id or email for bisque
    user_id             TEXT,
    username            TEXT,
    user_name           TEXT,

    -- Content
    text                TEXT,
    reply_to            TEXT,                      -- message id this replies to
    reply_to_message_id TEXT,                      -- outbound: telegram message id of the parent

    -- Media
    image_file          TEXT,
    image_width         INTEGER,
    image_height        INTEGER,
    audio_file          TEXT,
    audio_duration      INTEGER,
    audio_mime_type     TEXT,
    transcription       TEXT,
    transcribed_at      TEXT,
    transcription_model TEXT,
    file_path           TEXT,
    file_name           TEXT,
    mime_type           TEXT,
    file_size           INTEGER,

    -- Telegram-specific
    telegram_message_id TEXT,
    callback_data       TEXT,
    callback_query_id   TEXT,
    original_message_id TEXT,
    original_message_text TEXT,
    media_group_id      TEXT,

    -- Timestamps
    timestamp           TEXT NOT NULL,             -- ISO-8601 from the JSON file
    imported_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    -- Extra fields stored verbatim as JSON (rare/one-off keys)
    extra               TEXT                       -- JSON blob for overflow fields
);

CREATE INDEX IF NOT EXISTS idx_messages_timestamp  ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_direction  ON messages(direction);
CREATE INDEX IF NOT EXISTS idx_messages_source     ON messages(source);
CREATE INDEX IF NOT EXISTS idx_messages_type       ON messages(type);
CREATE INDEX IF NOT EXISTS idx_messages_chat_id    ON messages(chat_id);

-- FTS5 for keyword search over message text + transcription
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    transcription,
    user_name,
    source,
    type,
    content='messages',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_ai
    AFTER INSERT ON messages
BEGIN
    INSERT INTO messages_fts(rowid, text, transcription, user_name, source, type)
    VALUES (new.rowid, new.text, new.transcription, new.user_name, new.source, new.type);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_ad
    AFTER DELETE ON messages
BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text, transcription, user_name, source, type)
    VALUES ('delete', old.rowid, old.text, old.transcription, old.user_name, old.source, old.type);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_au
    AFTER UPDATE ON messages
BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text, transcription, user_name, source, type)
    VALUES ('delete', old.rowid, old.text, old.transcription, old.user_name, old.source, old.type);
    INSERT INTO messages_fts(rowid, text, transcription, user_name, source, type)
    VALUES (new.rowid, new.text, new.transcription, new.user_name, new.source, new.type);
END;

-- ─── bisque_events ───────────────────────────────────────────────────────────
-- Messages originating from the Bisque channel (source = 'bisque').
-- Contains bisque-specific fields not present on all messages.
CREATE TABLE IF NOT EXISTS bisque_events (
    id                  TEXT PRIMARY KEY,
    chat_id             TEXT,                      -- bisque email address
    type                TEXT,                      -- 'text' | 'voice' | 'subagent_result' | ...
    text                TEXT,
    reply_to_id         TEXT,
    reply_to            TEXT,

    -- Media (voice)
    audio_file          TEXT,
    transcription       TEXT,
    transcribed_at      TEXT,
    transcription_model TEXT,

    -- Bisque agent results (when type = subagent_result)
    task_id             TEXT,
    agent_id            TEXT,
    status              TEXT,
    sent_reply_to_user  INTEGER,                   -- BOOLEAN stored as 0/1
    attachments         TEXT,                      -- JSON array
    warning             TEXT,

    -- Timestamps
    timestamp           TEXT NOT NULL,
    imported_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_bisque_events_timestamp  ON bisque_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_bisque_events_type       ON bisque_events(type);
CREATE INDEX IF NOT EXISTS idx_bisque_events_chat_id    ON bisque_events(chat_id);
CREATE INDEX IF NOT EXISTS idx_bisque_events_task_id    ON bisque_events(task_id);

CREATE VIRTUAL TABLE IF NOT EXISTS bisque_events_fts USING fts5(
    text,
    transcription,
    type,
    content='bisque_events',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS bisque_events_fts_ai
    AFTER INSERT ON bisque_events
BEGIN
    INSERT INTO bisque_events_fts(rowid, text, transcription, type)
    VALUES (new.rowid, new.text, new.transcription, new.type);
END;

CREATE TRIGGER IF NOT EXISTS bisque_events_fts_ad
    AFTER DELETE ON bisque_events
BEGIN
    INSERT INTO bisque_events_fts(bisque_events_fts, rowid, text, transcription, type)
    VALUES ('delete', old.rowid, old.text, old.transcription, old.type);
END;

CREATE TRIGGER IF NOT EXISTS bisque_events_fts_au
    AFTER UPDATE ON bisque_events
BEGIN
    INSERT INTO bisque_events_fts(bisque_events_fts, rowid, text, transcription, type)
    VALUES ('delete', old.rowid, old.text, old.transcription, old.type);
    INSERT INTO bisque_events_fts(rowid, text, transcription, type)
    VALUES (new.rowid, new.text, new.transcription, new.type);
END;

-- ─── agent_events ────────────────────────────────────────────────────────────
-- Subagent lifecycle events: results, failures, notifications, errors.
-- Types: subagent_result | agent_failed | subagent_notification | subagent_error
--        task-notification
CREATE TABLE IF NOT EXISTS agent_events (
    id                  TEXT PRIMARY KEY,
    type                TEXT NOT NULL,             -- 'subagent_result' | 'agent_failed' | ...
    source              TEXT,                      -- 'telegram' | 'system' | 'internal'
    chat_id             TEXT,

    -- Task identity
    task_id             TEXT,
    agent_id            TEXT,

    -- Outcome
    status              TEXT,                      -- 'success' | 'error'
    text                TEXT,                      -- human-readable result summary
    sent_reply_to_user  INTEGER,                   -- BOOLEAN stored as 0/1
    warning             TEXT,

    -- Failure details
    original_chat_id    TEXT,
    original_prompt     TEXT,
    last_output         TEXT,

    -- Artifacts / forward
    artifacts           TEXT,                      -- JSON array of file paths
    forward             TEXT,                      -- JSON blob

    -- Timestamps
    timestamp           TEXT NOT NULL,
    imported_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_agent_events_timestamp  ON agent_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_agent_events_type       ON agent_events(type);
CREATE INDEX IF NOT EXISTS idx_agent_events_task_id    ON agent_events(task_id);
CREATE INDEX IF NOT EXISTS idx_agent_events_agent_id   ON agent_events(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_events_status     ON agent_events(status);
CREATE INDEX IF NOT EXISTS idx_agent_events_chat_id    ON agent_events(chat_id);

CREATE VIRTUAL TABLE IF NOT EXISTS agent_events_fts USING fts5(
    text,
    original_prompt,
    type,
    content='agent_events',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS agent_events_fts_ai
    AFTER INSERT ON agent_events
BEGIN
    INSERT INTO agent_events_fts(rowid, text, original_prompt, type)
    VALUES (new.rowid, new.text, new.original_prompt, new.type);
END;

CREATE TRIGGER IF NOT EXISTS agent_events_fts_ad
    AFTER DELETE ON agent_events
BEGIN
    INSERT INTO agent_events_fts(agent_events_fts, rowid, text, original_prompt, type)
    VALUES ('delete', old.rowid, old.text, old.original_prompt, old.type);
END;

CREATE TRIGGER IF NOT EXISTS agent_events_fts_au
    AFTER UPDATE ON agent_events
BEGIN
    INSERT INTO agent_events_fts(agent_events_fts, rowid, text, original_prompt, type)
    VALUES ('delete', old.rowid, old.text, old.original_prompt, old.type);
    INSERT INTO agent_events_fts(rowid, text, original_prompt, type)
    VALUES (new.rowid, new.text, new.original_prompt, new.type);
END;

-- ─── schema_migrations ───────────────────────────────────────────────────────
-- Simple migration tracking table.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    description TEXT
);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('001', 'Initial schema: messages, bisque_events, agent_events, FTS5');
