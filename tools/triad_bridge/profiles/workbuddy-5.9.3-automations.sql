-- Reviewed, non-personal schema profile: WorkBuddy 5.9.3 `automations` table.
-- This file only describes the software's table definition. It contains no
-- task rows, no owner identifiers and no local paths.
--
-- The bridge compares the whole statement, not just the column list: this text is
-- executed in memory, the stored `sqlite_master.sql` is tokenised, and the same
-- token stream must come back from the live database. Formatting and comments are
-- ignored, while a CHECK, a generated column, a changed constraint or a different
-- default is not. Spaces inside a quoted default ('[]' vs ' []') stay meaningful.
-- Column order/type/nullability and any extra UNIQUE index are checked as well.
CREATE TABLE automations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    schedule_type TEXT NOT NULL DEFAULT 'recurring',
    next_run_at INTEGER,
    last_run_at INTEGER,
    cwds TEXT NOT NULL DEFAULT '[]',
    rrule TEXT NOT NULL DEFAULT '',
    scheduled_at TEXT,
    valid_from TEXT,
    valid_until TEXT,
    model_id TEXT,
    model_is_thinking INTEGER NOT NULL DEFAULT 0,
    skills_json TEXT NOT NULL DEFAULT '[]',
    push_to_wechat INTEGER NOT NULL DEFAULT 0,
    push_to_wecom_bot INTEGER NOT NULL DEFAULT 0,
    wecom_bot_source TEXT,
    owner_user_id TEXT,
    owner_status TEXT NOT NULL DEFAULT 'legacy_unassigned',
    owner_source TEXT,
    expert_id TEXT,
    expert_marketplace TEXT,
    connector_ids_json TEXT NOT NULL DEFAULT '[]',
    permission_mode TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    deleted_at INTEGER,
    context_window INTEGER,
    reasoning_effort TEXT,
    workspace_scope TEXT,
    wecom_bot_chat_id TEXT
);
