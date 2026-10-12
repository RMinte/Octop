-- Schema v23: fixed Run claim and dispatch acceptance facts.
-- This journal does not enable model execution or bind a history archive Turn.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS ac_run_dispatches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dispatch_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL UNIQUE,
  turn_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
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

UPDATE _schema_version SET version = 23;

COMMIT;
