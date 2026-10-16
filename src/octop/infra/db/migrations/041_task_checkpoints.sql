-- Native task waits retain their checkpoint while releasing the worker lease.
ALTER TABLE project_task_turns ADD COLUMN checkpoint_id TEXT NOT NULL DEFAULT '';
ALTER TABLE project_task_turns ADD COLUMN pause_json TEXT NOT NULL DEFAULT '';
ALTER TABLE project_task_turns ADD COLUMN interactions_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE project_task_turns ADD COLUMN interaction_revision BIGINT NOT NULL DEFAULT 0;
ALTER TABLE project_task_turns ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0;
ALTER TABLE project_task_turns ADD COLUMN wait_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE project_task_turns ADD COLUMN requests_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE project_tasks ADD COLUMN wait_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE project_tasks ADD COLUMN approval_mode TEXT NOT NULL DEFAULT 'ask_for_approval';
ALTER TABLE project_tasks ADD COLUMN approval_revision BIGINT NOT NULL DEFAULT 1;
ALTER TABLE project_tasks ADD COLUMN approval_updated_at BIGINT NOT NULL DEFAULT 0;
ALTER TABLE project_tasks ADD COLUMN approval_requests_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE project_task_tool_calls ADD COLUMN approval_context_json TEXT NOT NULL DEFAULT '{}';
UPDATE _schema_version SET version = 41;

-- Independent, tool-free reviews are durable and bound to one exact action.
CREATE TABLE IF NOT EXISTS project_task_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL REFERENCES project_tasks(task_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL REFERENCES project_task_turns(turn_id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    execution_host TEXT NOT NULL CHECK (execution_host IN ('server', 'client')),
    target_id TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    model_ref TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'reviewing', 'completed', 'cancelled')),
    decision TEXT NOT NULL DEFAULT '',
    reason_code TEXT NOT NULL DEFAULT '',
    worker_id TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    UNIQUE(execution_host, target_id)
);
CREATE INDEX IF NOT EXISTS idx_project_task_reviews_queue ON project_task_reviews(status, id);
