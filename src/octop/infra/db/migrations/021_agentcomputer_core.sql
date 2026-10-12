-- Schema v21: AgentComputer command-admission core.
-- No dispatch, model execution, or user data migration is enabled by this schema.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS ac_projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL UNIQUE,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  box_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL UNIQUE,
  default_agent_id TEXT UNIQUE REFERENCES agents(agent_id),
  preparation_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (preparation_state IN ('pending', 'preparing', 'ready', 'failed')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  config_json TEXT NOT NULL,
  config_digest TEXT NOT NULL,
  active_run_id TEXT,
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
  UNIQUE (project_id, owner_id),
  CONSTRAINT ac_projects_active_run_fk FOREIGN KEY (active_run_id, project_id, owner_id)
    REFERENCES ac_runs(run_id, project_id, owner_id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS ac_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL UNIQUE,
  project_id TEXT NOT NULL,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  session_id TEXT NOT NULL UNIQUE,
  octop_thread_id TEXT UNIQUE REFERENCES threads(thread_id),
  preparation_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (preparation_state IN ('pending', 'preparing', 'ready', 'failed')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  next_sequence INTEGER NOT NULL DEFAULT 1 CHECK (next_sequence > 0),
  active_run_id TEXT,
  title TEXT NOT NULL,
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
  UNIQUE (task_id, owner_id),
  UNIQUE (task_id, project_id, owner_id),
  FOREIGN KEY (project_id, owner_id) REFERENCES ac_projects(project_id, owner_id),
  CONSTRAINT ac_tasks_active_run_fk FOREIGN KEY (active_run_id, task_id, owner_id)
    REFERENCES ac_runs(run_id, task_id, owner_id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS ac_turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  turn_id TEXT NOT NULL UNIQUE,
  task_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  user_message_id TEXT NOT NULL,
  assistant_message_id TEXT NOT NULL UNIQUE,
  context_mode TEXT NOT NULL CHECK (context_mode = 'newMessage'),
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  snapshot_json TEXT NOT NULL,
  snapshot_digest TEXT NOT NULL,
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  UNIQUE (task_id, user_message_id),
  UNIQUE (turn_id, task_id, owner_id),
  UNIQUE (turn_id, task_id, project_id, owner_id),
  FOREIGN KEY (task_id, project_id, owner_id)
    REFERENCES ac_tasks(task_id, project_id, owner_id)
);

CREATE TABLE IF NOT EXISTS ac_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL UNIQUE,
  turn_id TEXT NOT NULL UNIQUE,
  task_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  state TEXT NOT NULL CHECK (state IN (
    'queued', 'running', 'waitingApproval', 'waitingInput',
    'succeeded', 'failed', 'stopped', 'interrupted'
  )),
  dispatch_phase TEXT NOT NULL CHECK (dispatch_phase IN (
    'notDispatched', 'claimed', 'dispatching', 'engineAccepted'
  )),
  execution_epoch INTEGER NOT NULL DEFAULT 0 CHECK (execution_epoch >= 0),
  restore_epoch INTEGER NOT NULL DEFAULT 1 CHECK (restore_epoch > 0),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  reconciliation_required INTEGER NOT NULL DEFAULT 0 CHECK (reconciliation_required IN (0, 1)),
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
  UNIQUE (run_id, task_id, owner_id),
  UNIQUE (run_id, project_id, owner_id),
  UNIQUE (run_id, turn_id, task_id, owner_id),
  FOREIGN KEY (task_id, project_id, owner_id)
    REFERENCES ac_tasks(task_id, project_id, owner_id),
  FOREIGN KEY (turn_id, task_id, project_id, owner_id)
    REFERENCES ac_turns(turn_id, task_id, project_id, owner_id)
);

CREATE TABLE IF NOT EXISTS ac_commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  command_id TEXT NOT NULL UNIQUE,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  idempotency_key TEXT NOT NULL,
  operation TEXT NOT NULL,
  target_id TEXT NOT NULL,
  payload_digest TEXT NOT NULL,
  restore_epoch INTEGER NOT NULL CHECK (restore_epoch > 0),
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  receipt_json TEXT NOT NULL,
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  UNIQUE (owner_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS ac_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  task_id TEXT NOT NULL,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  run_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_version INTEGER NOT NULL CHECK (payload_version > 0),
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  payload_json TEXT NOT NULL,
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  UNIQUE (task_id, sequence),
  FOREIGN KEY (task_id, owner_id) REFERENCES ac_tasks(task_id, owner_id),
  FOREIGN KEY (run_id, turn_id, task_id, owner_id)
    REFERENCES ac_runs(run_id, turn_id, task_id, owner_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ac_runs_one_active_per_task
  ON ac_runs(task_id)
  WHERE state IN ('queued', 'running', 'waitingApproval', 'waitingInput')
    OR reconciliation_required = 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ac_runs_one_active_per_project
  ON ac_runs(project_id)
  WHERE state IN ('queued', 'running', 'waitingApproval', 'waitingInput')
    OR reconciliation_required = 1;

CREATE INDEX IF NOT EXISTS idx_ac_projects_owner ON ac_projects(owner_id, project_id);
CREATE INDEX IF NOT EXISTS idx_ac_tasks_project ON ac_tasks(project_id, task_id);
CREATE INDEX IF NOT EXISTS idx_ac_runs_task ON ac_runs(task_id, created_at);

UPDATE _schema_version SET version = 21;

COMMIT;
