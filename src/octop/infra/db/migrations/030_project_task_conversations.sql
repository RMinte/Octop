-- Independent task conversations; no expert/agent foreign key.
CREATE TABLE IF NOT EXISTS project_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL UNIQUE,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  project_id TEXT REFERENCES projects(project_id),
  title TEXT NOT NULL,
  request_id TEXT NOT NULL,
  active_turn_id TEXT,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  UNIQUE(owner_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_project_tasks_owner_updated ON project_tasks(owner_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_project_tasks_project ON project_tasks(project_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS project_task_turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  turn_id TEXT NOT NULL UNIQUE,
  task_id TEXT NOT NULL REFERENCES project_tasks(task_id),
  request_id TEXT NOT NULL,
  prompt TEXT NOT NULL,
  reply TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
  error_code TEXT NOT NULL DEFAULT '',
  activity TEXT NOT NULL DEFAULT '',
  configuration_json TEXT NOT NULL,
  model_ref TEXT NOT NULL,
  worker_id TEXT NOT NULL DEFAULT '',
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  UNIQUE(task_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_project_task_turns_task ON project_task_turns(task_id, id);
CREATE INDEX IF NOT EXISTS idx_project_task_turns_queue ON project_task_turns(status, created_at);
UPDATE _schema_version SET version = 30;
