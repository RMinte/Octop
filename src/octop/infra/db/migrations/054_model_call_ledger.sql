-- Independent invocation receipts; intentionally excluded from legacy usage totals.
CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL UNIQUE,
    parent_call_id TEXT NOT NULL DEFAULT '',
    submission_id TEXT NOT NULL DEFAULT '',
    user_id INTEGER,
    agent_id TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    project_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model_ref TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'unknown'
        CHECK (source IN ('box', 'third_party', 'self_hosted', 'unknown')),
    purpose TEXT NOT NULL DEFAULT 'unknown',
    started_at BIGINT NOT NULL,
    finished_at BIGINT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled', 'interrupted')),
    usage_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_model_calls_user ON model_calls(user_id, id);
CREATE INDEX IF NOT EXISTS idx_model_calls_task ON model_calls(task_id, id);
CREATE INDEX IF NOT EXISTS idx_model_calls_thread ON model_calls(thread_id, id);
CREATE INDEX IF NOT EXISTS idx_model_calls_turn ON model_calls(turn_id, id);
CREATE INDEX IF NOT EXISTS idx_model_calls_submission ON model_calls(submission_id, id);
CREATE INDEX IF NOT EXISTS idx_model_calls_status ON model_calls(status, id);
UPDATE _schema_version SET version = 54;
