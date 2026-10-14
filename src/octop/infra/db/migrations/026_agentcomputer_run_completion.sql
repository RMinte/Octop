-- Schema v26: immutable natural-completion receipts for trusted, no-tool Runs.
-- A receipt is written atomically with the Run terminal state, event and lane release.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS ac_run_terminals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL UNIQUE,
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
  receipt_json TEXT NOT NULL,
  receipt_digest TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
  completed_at INTEGER NOT NULL CHECK (typeof(completed_at) = 'integer' AND completed_at >= 0),
  FOREIGN KEY (run_id, turn_id, task_id, owner_id)
    REFERENCES ac_runs(run_id, turn_id, task_id, owner_id),
  FOREIGN KEY (run_id, project_id, owner_id)
    REFERENCES ac_runs(run_id, project_id, owner_id),
  FOREIGN KEY (dispatch_id, run_id, turn_id, task_id, project_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id)
    REFERENCES ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, project_id, owner_id,
      restore_epoch, execution_epoch, claimant_id, engine_request_id)
);

UPDATE _schema_version SET version = 26;

COMMIT;
