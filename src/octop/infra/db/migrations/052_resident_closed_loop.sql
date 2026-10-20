-- Historical days, explicit task handoffs and durable external results.
ALTER TABLE resident_requests ADD COLUMN day_key TEXT NOT NULL DEFAULT '';
ALTER TABLE resident_requests ADD COLUMN timezone TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_resident_requests_days
  ON resident_requests(owner_id,thread_id,day_key,created_at DESC,turn_id DESC);
CREATE INDEX IF NOT EXISTS idx_resident_requests_timeline
  ON resident_requests(owner_id,thread_id,created_at DESC,turn_id DESC);
CREATE TABLE IF NOT EXISTS resident_delegations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  delegation_id TEXT NOT NULL UNIQUE,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  origin_agent_id TEXT NOT NULL,
  origin_thread_id TEXT NOT NULL REFERENCES resident_conversations(thread_id),
  source_turn_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  initial_turn_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  timezone TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  UNIQUE(owner_id,request_id)
);
CREATE INDEX IF NOT EXISTS idx_resident_delegations_thread
  ON resident_delegations(owner_id,origin_thread_id,id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_resident_delegations_initial_turn
  ON resident_delegations(initial_turn_id);
CREATE TABLE IF NOT EXISTS resident_task_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT NOT NULL UNIQUE,
  message_id TEXT NOT NULL UNIQUE,
  delegation_id TEXT NOT NULL REFERENCES resident_delegations(delegation_id),
  owner_id INTEGER NOT NULL REFERENCES users(id),
  thread_id TEXT NOT NULL REFERENCES resident_conversations(thread_id),
  task_id TEXT NOT NULL,
  task_turn_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('completed','failed','cancelled')),
  text TEXT NOT NULL DEFAULT '',
  error_code TEXT NOT NULL DEFAULT '',
  artifacts_json TEXT NOT NULL DEFAULT '[]',
  day_key TEXT NOT NULL,
  timezone TEXT NOT NULL,
  captured_at BIGINT NOT NULL,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK(delivery_status IN ('pending','delivered')),
  archive_written_at BIGINT,
  context_written_at BIGINT,
  delivery_error TEXT NOT NULL DEFAULT '',
  message_json TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_resident_task_reports_pending
  ON resident_task_reports(delivery_status,id);
CREATE INDEX IF NOT EXISTS idx_resident_task_reports_thread
  ON resident_task_reports(owner_id,thread_id,captured_at DESC,report_id DESC);
CREATE INDEX IF NOT EXISTS idx_resident_task_reports_days
  ON resident_task_reports(owner_id,thread_id,day_key,captured_at DESC,report_id DESC);
UPDATE _schema_version SET version = CASE WHEN version < 52 THEN 52 ELSE version END;
