-- Schema v25: durable Run-bound approvals and questions.
-- A decision is an adjudication fact, not engine-resume or action authorization.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS ac_run_pauses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pause_id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('approval', 'question')),
  run_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
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
  FOREIGN KEY (dispatch_id, run_id, turn_id, task_id, project_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id)
    REFERENCES ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, project_id, owner_id,
      restore_epoch, execution_epoch, claimant_id, engine_request_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ac_run_pauses_one_active_per_run
  ON ac_run_pauses(run_id) WHERE phase IN ('pending', 'decided');

CREATE UNIQUE INDEX IF NOT EXISTS idx_ac_run_pauses_decision_key
  ON ac_run_pauses(owner_id, decision_idempotency_key)
  WHERE decision_idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS ac_run_cancellations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL UNIQUE,
  turn_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
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

UPDATE _schema_version SET version = 25;

COMMIT;
