-- Daily operational-activity snapshots, separate from monthly scored history.
-- Each --mode daily run upserts one row per (agent_id, snapshot_date,
-- metric_key). snapshot_date is ISO YYYY-MM-DD. Metric values are cumulative
-- month-to-date for that agent on that day (rate metrics are MTD averages,
-- count metrics are MTD totals). Today/Week views are derived in the read
-- path by diffing snapshots — keeps storage simple and idempotent.

CREATE TABLE IF NOT EXISTS agent_daily_snapshots (
    agent_id        TEXT NOT NULL,
    snapshot_date   TEXT NOT NULL,           -- ISO YYYY-MM-DD
    metric_key      TEXT NOT NULL,
    value           REAL,
    captured_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (agent_id, snapshot_date, metric_key)
);

CREATE INDEX IF NOT EXISTS idx_daily_snapshots_date
    ON agent_daily_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_daily_snapshots_agent_date
    ON agent_daily_snapshots(agent_id, snapshot_date);
