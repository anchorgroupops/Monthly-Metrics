-- Self-serve agent portal: magic-link login + persistent sessions.
-- Adds two tables that live alongside the existing admin tables. Agent
-- identity comes from agent_meta.email (case-insensitive lookup); we don't
-- store passwords for agents.

CREATE TABLE IF NOT EXISTS portal_magic_links (
    token       TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_portal_magic_email ON portal_magic_links(email);

CREATE TABLE IF NOT EXISTS portal_sessions (
    token       TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (agent_id) REFERENCES agent_meta(agent_id)
);
CREATE INDEX IF NOT EXISTS idx_portal_session_agent ON portal_sessions(agent_id);
