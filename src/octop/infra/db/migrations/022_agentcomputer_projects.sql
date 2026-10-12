-- Schema v22: durable project preparation without execution readiness.

BEGIN IMMEDIATE;

ALTER TABLE ac_projects ADD COLUMN name TEXT NOT NULL DEFAULT '';
ALTER TABLE agents ADD COLUMN managed_by TEXT
  CHECK (managed_by IS NULL OR managed_by = 'agentcomputer');
ALTER TABLE agents ADD COLUMN managed_project_id TEXT REFERENCES ac_projects(project_id)
  CONSTRAINT agents_managed_project_pair CHECK (
    (managed_by IS NULL AND managed_project_id IS NULL)
    OR (managed_by IS NOT NULL AND managed_by = 'agentcomputer' AND managed_project_id IS NOT NULL)
  );

CREATE TABLE IF NOT EXISTS ac_project_preparations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  preparation_id TEXT NOT NULL UNIQUE,
  project_id TEXT NOT NULL UNIQUE,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  planned_agent_id TEXT NOT NULL UNIQUE,
  intent_json TEXT NOT NULL,
  intent_digest TEXT NOT NULL,
  restore_epoch INTEGER NOT NULL CHECK (restore_epoch > 0),
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
  agent_state TEXT NOT NULL DEFAULT 'pending' CHECK (agent_state IN ('pending', 'verified')),
  workspace_state TEXT NOT NULL DEFAULT 'pending' CHECK (workspace_state IN ('pending', 'verified')),
  reason_code TEXT,
  retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
  created_at INTEGER NOT NULL CHECK (created_at >= 0),
  updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
  FOREIGN KEY (project_id, owner_id) REFERENCES ac_projects(project_id, owner_id)
);

UPDATE _schema_version SET version = 22;

COMMIT;
