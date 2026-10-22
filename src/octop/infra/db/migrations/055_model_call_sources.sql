-- Product sources are the explicit box connection and all other providers.
BEGIN IMMEDIATE;
CREATE TABLE model_calls_v55 (
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
    source TEXT NOT NULL DEFAULT 'third_party'
        CHECK (source IN ('box', 'third_party')),
    purpose TEXT NOT NULL DEFAULT 'unknown',
    started_at BIGINT NOT NULL,
    finished_at BIGINT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled', 'interrupted')),
    usage_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
INSERT INTO model_calls_v55 (
    id, call_id, parent_call_id, submission_id, user_id, agent_id, thread_id,
    project_id, task_id, turn_id, provider, model_ref, source, purpose,
    started_at, finished_at, status, usage_json, metadata_json
)
SELECT id, call_id, parent_call_id, submission_id, user_id, agent_id, thread_id,
    project_id, task_id, turn_id, provider, model_ref,
    CASE WHEN source = 'box' THEN 'box' ELSE 'third_party' END,
    purpose, started_at, finished_at, status, usage_json, metadata_json
FROM model_calls;
-- Preserve pagination IDs even if the former highest receipt was removed.
UPDATE sqlite_sequence SET seq = MAX(seq, COALESCE(
    (SELECT seq FROM sqlite_sequence WHERE name = 'model_calls'), 0
)) WHERE name = 'model_calls_v55';
DROP TABLE model_calls;
ALTER TABLE model_calls_v55 RENAME TO model_calls;
CREATE INDEX idx_model_calls_user ON model_calls(user_id, id);
CREATE INDEX idx_model_calls_task ON model_calls(task_id, id);
CREATE INDEX idx_model_calls_thread ON model_calls(thread_id, id);
CREATE INDEX idx_model_calls_turn ON model_calls(turn_id, id);
CREATE INDEX idx_model_calls_submission ON model_calls(submission_id, id);
CREATE INDEX idx_model_calls_status ON model_calls(status, id);
UPDATE _schema_version SET version = 55;
COMMIT;
