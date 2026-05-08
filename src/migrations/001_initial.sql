CREATE TABLE IF NOT EXISTS agent_periods (
    agent_id     TEXT NOT NULL,
    period       TEXT NOT NULL,
    metric_key   TEXT NOT NULL,
    value        REAL,
    raw_json     TEXT,
    ingested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (agent_id, period, metric_key)
);

CREATE TABLE IF NOT EXISTS agent_meta (
    agent_id     TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    email        TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period       TEXT NOT NULL,
    source       TEXT NOT NULL,
    file_path    TEXT,
    row_count    INTEGER,
    status       TEXT NOT NULL,
    notes        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT NOT NULL,
    period       TEXT NOT NULL,
    html         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at  TEXT,
    sent_at      TEXT,
    UNIQUE (agent_id, period)
);

CREATE INDEX IF NOT EXISTS idx_periods_agent ON agent_periods(agent_id, period);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(period, status);
