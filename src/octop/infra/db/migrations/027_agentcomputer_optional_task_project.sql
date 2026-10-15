-- Schema v27: a Task may own its execution context without a project.
-- Execution journals gain nullable project IDs and mandatory Task/owner lineage.
-- Their runtime API remains closed until independent execution proofs are ready.
-- Rebuilds preserve public IDs and existing project-scoped foreign keys.

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TABLE ac_tasks_v27 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL UNIQUE,
  project_id TEXT,
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
  standalone_box_id TEXT,
  standalone_workspace_id TEXT UNIQUE,
  standalone_agent_id TEXT UNIQUE,
  standalone_config_json TEXT,
  standalone_config_digest TEXT,
  UNIQUE (task_id, owner_id),
  UNIQUE (task_id, project_id, owner_id),
  CONSTRAINT ac_tasks_scope_shape CHECK (
    (project_id IS NOT NULL AND standalone_box_id IS NULL
      AND standalone_workspace_id IS NULL AND standalone_agent_id IS NULL
      AND standalone_config_json IS NULL AND standalone_config_digest IS NULL)
    OR (project_id IS NULL AND standalone_box_id IS NOT NULL
      AND standalone_workspace_id IS NOT NULL AND standalone_agent_id IS NOT NULL
      AND standalone_config_json IS NOT NULL AND standalone_config_digest IS NOT NULL)
  ),
  FOREIGN KEY (project_id, owner_id) REFERENCES ac_projects(project_id, owner_id),
  CONSTRAINT ac_tasks_active_run_fk FOREIGN KEY (active_run_id, task_id, owner_id)
    REFERENCES ac_runs(run_id, task_id, owner_id) DEFERRABLE INITIALLY DEFERRED
);

INSERT INTO ac_tasks_v27 (
  id, task_id, project_id, owner_id, session_id, octop_thread_id,
  preparation_state, revision, schema_version, next_sequence, active_run_id,
  title, created_at, updated_at
)
SELECT
  id, task_id, project_id, owner_id, session_id, octop_thread_id,
  preparation_state, revision, schema_version, next_sequence, active_run_id,
  title, created_at, updated_at
FROM ac_tasks;

DROP TABLE ac_tasks;
ALTER TABLE ac_tasks_v27 RENAME TO ac_tasks;
CREATE INDEX idx_ac_tasks_project ON ac_tasks(project_id, task_id);

CREATE TABLE agents_v27 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL UNIQUE,
  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT,
  persona_mbti TEXT,
  default_model TEXT,
  system_prompt TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  config_json TEXT,
  last_state TEXT,
  last_error TEXT,
  icon TEXT,
  template_name TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  is_shared INTEGER NOT NULL DEFAULT 0,
  color TEXT,
  icon_name TEXT,
  icon_url TEXT,
  skill_package_ids TEXT,
  published_expert_id TEXT,
  welcome_message TEXT,
  knowledge_base_ids TEXT,
  mcp_servers TEXT,
  kind TEXT NOT NULL DEFAULT 'expert',
  managed_by TEXT CHECK (managed_by IS NULL OR managed_by = 'agentcomputer'),
  managed_project_id TEXT REFERENCES ac_projects(project_id),
  managed_task_id TEXT UNIQUE REFERENCES ac_tasks(task_id),
  CONSTRAINT agents_managed_scope_pair CHECK (
    (managed_by IS NULL AND managed_project_id IS NULL AND managed_task_id IS NULL)
    OR (managed_by IS NOT NULL AND managed_by = 'agentcomputer' AND (
      (managed_project_id IS NOT NULL AND managed_task_id IS NULL)
      OR (managed_project_id IS NULL AND managed_task_id IS NOT NULL)
    ))
  )
);

INSERT INTO agents_v27 (
  id, agent_id, user_id, name, description, persona_mbti, default_model,
  system_prompt, enabled, config_json, last_state, last_error, icon,
  template_name, created_at, updated_at, is_shared, color, icon_name,
  icon_url, skill_package_ids, published_expert_id, welcome_message,
  knowledge_base_ids, mcp_servers, kind, managed_by, managed_project_id
)
SELECT
  id, agent_id, user_id, name, description, persona_mbti, default_model,
  system_prompt, enabled, config_json, last_state, last_error, icon,
  template_name, created_at, updated_at, is_shared, color, icon_name,
  icon_url, skill_package_ids, published_expert_id, welcome_message,
  knowledge_base_ids, mcp_servers, kind, managed_by, managed_project_id
FROM agents;

DROP TABLE agents;
ALTER TABLE agents_v27 RENAME TO agents;
CREATE UNIQUE INDEX idx_agents_user_name ON agents(user_id, name) WHERE user_id IS NOT NULL;
CREATE INDEX idx_agents_shared ON agents(is_shared) WHERE is_shared = 1;

-- V27_EXECUTION_SCOPE_BEGIN
-- Rebuild project-bound journals with optional project scope.

CREATE TABLE ac_turns_v27 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  turn_id TEXT NOT NULL UNIQUE,
  task_id TEXT NOT NULL,
  project_id TEXT,
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
  FOREIGN KEY (task_id, owner_id) REFERENCES ac_tasks(task_id, owner_id),
  FOREIGN KEY (task_id, project_id, owner_id)
    REFERENCES ac_tasks(task_id, project_id, owner_id)
);
INSERT INTO ac_turns_v27 SELECT * FROM ac_turns;
DROP TABLE ac_turns;
ALTER TABLE ac_turns_v27 RENAME TO ac_turns;

CREATE TABLE ac_runs_v27 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL UNIQUE,
  turn_id TEXT NOT NULL UNIQUE,
  task_id TEXT NOT NULL,
  project_id TEXT,
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
  FOREIGN KEY (task_id, owner_id) REFERENCES ac_tasks(task_id, owner_id),
  FOREIGN KEY (turn_id, task_id, owner_id)
    REFERENCES ac_turns(turn_id, task_id, owner_id),
  FOREIGN KEY (task_id, project_id, owner_id)
    REFERENCES ac_tasks(task_id, project_id, owner_id),
  FOREIGN KEY (turn_id, task_id, project_id, owner_id)
    REFERENCES ac_turns(turn_id, task_id, project_id, owner_id)
);
INSERT INTO ac_runs_v27 SELECT * FROM ac_runs;
DROP TABLE ac_runs;
ALTER TABLE ac_runs_v27 RENAME TO ac_runs;

CREATE TABLE ac_run_dispatches_v27 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dispatch_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL UNIQUE,
  turn_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  project_id TEXT,
  owner_id INTEGER NOT NULL REFERENCES users(id)
    CHECK (typeof(owner_id) = 'integer' AND owner_id > 0),
  execution_epoch INTEGER NOT NULL CHECK (typeof(execution_epoch) = 'integer' AND execution_epoch > 0),
  restore_epoch INTEGER NOT NULL CHECK (typeof(restore_epoch) = 'integer' AND restore_epoch > 0),
  claimant_id TEXT NOT NULL,
  engine_request_id TEXT NOT NULL UNIQUE,
  intent_json TEXT NOT NULL,
  intent_digest TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(schema_version) = 'integer' AND schema_version > 0),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision) = 'integer' AND revision > 0),
  created_at INTEGER NOT NULL CHECK (typeof(created_at) = 'integer' AND created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (typeof(updated_at) = 'integer' AND updated_at >= 0),
  dispatch_started_at INTEGER CHECK (dispatch_started_at IS NULL OR (typeof(dispatch_started_at) = 'integer' AND dispatch_started_at >= 0)),
  engine_receipt_id TEXT UNIQUE,
  engine_receipt_json TEXT,
  engine_receipt_digest TEXT,
  engine_accepted_at INTEGER CHECK (engine_accepted_at IS NULL OR (typeof(engine_accepted_at) = 'integer' AND engine_accepted_at >= 0)),
  CHECK (
    (engine_receipt_id IS NULL AND engine_receipt_json IS NULL
      AND engine_receipt_digest IS NULL AND engine_accepted_at IS NULL)
    OR (engine_receipt_id IS NOT NULL AND engine_receipt_json IS NOT NULL
      AND engine_receipt_digest IS NOT NULL AND engine_accepted_at IS NOT NULL
      AND dispatch_started_at IS NOT NULL)
  ),
  FOREIGN KEY (run_id, turn_id, task_id, owner_id)
    REFERENCES ac_runs(run_id, turn_id, task_id, owner_id),
  FOREIGN KEY (run_id, project_id, owner_id)
    REFERENCES ac_runs(run_id, project_id, owner_id)
);
INSERT INTO ac_run_dispatches_v27 SELECT * FROM ac_run_dispatches;
DROP TABLE ac_run_dispatches;
ALTER TABLE ac_run_dispatches_v27 RENAME TO ac_run_dispatches;

CREATE TABLE ac_tool_effects_v27 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  effect_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  project_id TEXT,
  owner_id INTEGER NOT NULL REFERENCES users(id)
    CHECK (typeof(owner_id) = 'integer' AND owner_id > 0),
  dispatch_id TEXT NOT NULL,
  engine_request_id TEXT NOT NULL,
  claimant_id TEXT NOT NULL,
  restore_epoch INTEGER NOT NULL CHECK (typeof(restore_epoch) = 'integer' AND restore_epoch > 0),
  execution_epoch INTEGER NOT NULL CHECK (typeof(execution_epoch) = 'integer' AND execution_epoch > 0),
  tool_call_id TEXT NOT NULL,
  execution_attempt INTEGER NOT NULL CHECK (typeof(execution_attempt) = 'integer' AND execution_attempt = 1),
  phase TEXT NOT NULL DEFAULT 'prepared' CHECK (phase IN ('prepared', 'dispatching', 'terminal', 'outcomeUnknown')),
  intent_json TEXT NOT NULL,
  intent_digest TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(schema_version) = 'integer' AND schema_version > 0),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision) = 'integer' AND revision > 0),
  created_at INTEGER NOT NULL CHECK (typeof(created_at) = 'integer' AND created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (typeof(updated_at) = 'integer' AND updated_at >= 0),
  dispatch_started_at INTEGER CHECK (dispatch_started_at IS NULL OR (typeof(dispatch_started_at) = 'integer' AND dispatch_started_at >= 0)),
  terminal_json TEXT,
  terminal_digest TEXT,
  terminal_at INTEGER CHECK (terminal_at IS NULL OR (typeof(terminal_at) = 'integer' AND terminal_at >= 0)),
  CHECK (
    (phase = 'terminal' AND terminal_json IS NOT NULL AND terminal_digest IS NOT NULL AND terminal_at IS NOT NULL)
    OR (phase <> 'terminal' AND terminal_json IS NULL AND terminal_digest IS NULL AND terminal_at IS NULL)
  ),
  CHECK (phase <> 'prepared' OR dispatch_started_at IS NULL),
  CHECK (phase <> 'dispatching' OR dispatch_started_at IS NOT NULL),
  UNIQUE (run_id, tool_call_id, execution_attempt),
  FOREIGN KEY (run_id, turn_id, task_id, owner_id)
    REFERENCES ac_runs(run_id, turn_id, task_id, owner_id),
  FOREIGN KEY (run_id, project_id, owner_id)
    REFERENCES ac_runs(run_id, project_id, owner_id),
  FOREIGN KEY (dispatch_id, run_id, turn_id, task_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id)
    REFERENCES ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, owner_id,
      restore_epoch, execution_epoch, claimant_id, engine_request_id),
  FOREIGN KEY (dispatch_id, run_id, turn_id, task_id, project_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id)
    REFERENCES ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, project_id, owner_id,
      restore_epoch, execution_epoch, claimant_id, engine_request_id)
);
INSERT INTO ac_tool_effects_v27 SELECT * FROM ac_tool_effects;
DROP TABLE ac_tool_effects;
ALTER TABLE ac_tool_effects_v27 RENAME TO ac_tool_effects;

CREATE TABLE ac_run_pauses_v27 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pause_id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('approval', 'question')),
  run_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  project_id TEXT,
  owner_id INTEGER NOT NULL REFERENCES users(id)
    CHECK (typeof(owner_id) = 'integer' AND owner_id > 0),
  dispatch_id TEXT NOT NULL,
  engine_request_id TEXT NOT NULL,
  claimant_id TEXT NOT NULL,
  restore_epoch INTEGER NOT NULL CHECK (typeof(restore_epoch) = 'integer' AND restore_epoch > 0),
  execution_epoch INTEGER NOT NULL CHECK (typeof(execution_epoch) = 'integer' AND execution_epoch > 0),
  phase TEXT NOT NULL DEFAULT 'pending' CHECK (phase IN ('pending', 'decided', 'expired')),
  intent_json TEXT NOT NULL,
  intent_digest TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(schema_version) = 'integer' AND schema_version > 0),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision) = 'integer' AND revision > 0),
  created_at INTEGER NOT NULL CHECK (typeof(created_at) = 'integer' AND created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (typeof(updated_at) = 'integer' AND updated_at >= 0),
  expires_at INTEGER NOT NULL CHECK (typeof(expires_at) = 'integer' AND expires_at > created_at),
  decision_command_id TEXT UNIQUE,
  decision_idempotency_key TEXT,
  decision_request_json TEXT,
  decision_request_digest TEXT,
  decision_receipt_json TEXT,
  decision_receipt_digest TEXT,
  decided_at INTEGER CHECK (decided_at IS NULL OR (typeof(decided_at) = 'integer' AND decided_at >= 0)),
  CHECK (
    (phase IN ('decided', 'expired') AND decision_command_id IS NOT NULL
      AND decision_idempotency_key IS NOT NULL AND decision_request_json IS NOT NULL
      AND decision_request_digest IS NOT NULL AND decision_receipt_json IS NOT NULL
      AND decision_receipt_digest IS NOT NULL AND decided_at IS NOT NULL)
    OR (phase IN ('pending', 'expired') AND decision_command_id IS NULL
      AND decision_idempotency_key IS NULL AND decision_request_json IS NULL
      AND decision_request_digest IS NULL AND decision_receipt_json IS NULL
      AND decision_receipt_digest IS NULL AND decided_at IS NULL)
  ),
  FOREIGN KEY (run_id, turn_id, task_id, owner_id)
    REFERENCES ac_runs(run_id, turn_id, task_id, owner_id),
  FOREIGN KEY (run_id, project_id, owner_id)
    REFERENCES ac_runs(run_id, project_id, owner_id),
  FOREIGN KEY (dispatch_id, run_id, turn_id, task_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id)
    REFERENCES ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, owner_id,
      restore_epoch, execution_epoch, claimant_id, engine_request_id),
  FOREIGN KEY (dispatch_id, run_id, turn_id, task_id, project_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id)
    REFERENCES ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, project_id, owner_id,
      restore_epoch, execution_epoch, claimant_id, engine_request_id)
);
INSERT INTO ac_run_pauses_v27 SELECT * FROM ac_run_pauses;
DROP TABLE ac_run_pauses;
ALTER TABLE ac_run_pauses_v27 RENAME TO ac_run_pauses;

CREATE TABLE ac_run_cancellations_v27 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL UNIQUE,
  turn_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  project_id TEXT,
  owner_id INTEGER NOT NULL REFERENCES users(id)
    CHECK (typeof(owner_id) = 'integer' AND owner_id > 0),
  command_id TEXT NOT NULL UNIQUE REFERENCES ac_commands(command_id),
  restore_epoch INTEGER NOT NULL CHECK (typeof(restore_epoch) = 'integer' AND restore_epoch > 0),
  execution_epoch INTEGER NOT NULL CHECK (typeof(execution_epoch) = 'integer' AND execution_epoch >= 0),
  run_revision_at_request INTEGER NOT NULL
    CHECK (typeof(run_revision_at_request) = 'integer' AND run_revision_at_request > 0),
  phase TEXT NOT NULL CHECK (phase IN ('requested', 'stopped')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision) = 'integer' AND revision > 0),
  requested_at INTEGER NOT NULL CHECK (typeof(requested_at) = 'integer' AND requested_at >= 0),
  stopped_at INTEGER CHECK (stopped_at IS NULL OR (typeof(stopped_at) = 'integer' AND stopped_at >= 0)),
  CHECK (
    (phase = 'requested' AND stopped_at IS NULL)
    OR (phase = 'stopped' AND stopped_at IS NOT NULL AND stopped_at >= requested_at)
  ),
  FOREIGN KEY (run_id, turn_id, task_id, owner_id)
    REFERENCES ac_runs(run_id, turn_id, task_id, owner_id),
  FOREIGN KEY (run_id, project_id, owner_id)
    REFERENCES ac_runs(run_id, project_id, owner_id)
);
INSERT INTO ac_run_cancellations_v27 SELECT * FROM ac_run_cancellations;
DROP TABLE ac_run_cancellations;
ALTER TABLE ac_run_cancellations_v27 RENAME TO ac_run_cancellations;

CREATE TABLE ac_run_terminals_v27 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL UNIQUE,
  turn_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  project_id TEXT,
  owner_id INTEGER NOT NULL REFERENCES users(id)
    CHECK (typeof(owner_id) = 'integer' AND owner_id > 0),
  dispatch_id TEXT NOT NULL,
  engine_request_id TEXT NOT NULL,
  claimant_id TEXT NOT NULL,
  restore_epoch INTEGER NOT NULL CHECK (typeof(restore_epoch) = 'integer' AND restore_epoch > 0),
  execution_epoch INTEGER NOT NULL CHECK (typeof(execution_epoch) = 'integer' AND execution_epoch > 0),
  receipt_json TEXT NOT NULL,
  receipt_digest TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
  completed_at INTEGER NOT NULL CHECK (typeof(completed_at) = 'integer' AND completed_at >= 0),
  FOREIGN KEY (run_id, turn_id, task_id, owner_id)
    REFERENCES ac_runs(run_id, turn_id, task_id, owner_id),
  FOREIGN KEY (run_id, project_id, owner_id)
    REFERENCES ac_runs(run_id, project_id, owner_id),
  FOREIGN KEY (dispatch_id, run_id, turn_id, task_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id)
    REFERENCES ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, owner_id,
      restore_epoch, execution_epoch, claimant_id, engine_request_id),
  FOREIGN KEY (dispatch_id, run_id, turn_id, task_id, project_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id)
    REFERENCES ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, project_id, owner_id,
      restore_epoch, execution_epoch, claimant_id, engine_request_id)
);
INSERT INTO ac_run_terminals_v27 SELECT * FROM ac_run_terminals;
DROP TABLE ac_run_terminals;
ALTER TABLE ac_run_terminals_v27 RENAME TO ac_run_terminals;

CREATE UNIQUE INDEX idx_ac_runs_one_active_per_task
  ON ac_runs(task_id)
  WHERE state IN ('queued', 'running', 'waitingApproval', 'waitingInput')
    OR reconciliation_required = 1;
CREATE UNIQUE INDEX idx_ac_runs_one_active_per_project
  ON ac_runs(project_id)
  WHERE state IN ('queued', 'running', 'waitingApproval', 'waitingInput')
    OR reconciliation_required = 1;
CREATE INDEX idx_ac_runs_task ON ac_runs(task_id, created_at);
CREATE UNIQUE INDEX idx_ac_run_dispatches_effect_binding
  ON ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, project_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id);
CREATE UNIQUE INDEX idx_ac_run_dispatches_effect_binding_task
  ON ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id);
CREATE UNIQUE INDEX idx_ac_tool_effects_one_pending_per_run
  ON ac_tool_effects(run_id)
  WHERE phase IN ('prepared', 'dispatching', 'outcomeUnknown');
CREATE UNIQUE INDEX idx_ac_run_pauses_one_active_per_run
  ON ac_run_pauses(run_id) WHERE phase IN ('pending', 'decided');
CREATE UNIQUE INDEX idx_ac_run_pauses_decision_key
  ON ac_run_pauses(owner_id, decision_idempotency_key)
  WHERE decision_idempotency_key IS NOT NULL;

UPDATE _schema_version SET version = 27;

COMMIT;
PRAGMA foreign_keys = ON;
