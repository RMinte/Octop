-- Dedicated private Agent identity and durable text-only conversation receipts.
CREATE TABLE IF NOT EXISTS resident_conversations (
  owner_id INTEGER NOT NULL REFERENCES users(id),
  role TEXT NOT NULL DEFAULT 'primary' CHECK (role = 'primary'),
  agent_id TEXT NOT NULL UNIQUE REFERENCES agents(agent_id),
  thread_id TEXT NOT NULL UNIQUE REFERENCES threads(thread_id),
  status TEXT NOT NULL DEFAULT 'initializing' CHECK (status IN ('initializing','ready','failed')),
  error_code TEXT NOT NULL DEFAULT '',
  revision BIGINT NOT NULL DEFAULT 1,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  PRIMARY KEY (owner_id, role)
);
CREATE TABLE IF NOT EXISTS resident_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  turn_id TEXT NOT NULL UNIQUE,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  thread_id TEXT NOT NULL REFERENCES resident_conversations(thread_id),
  prompt TEXT NOT NULL,
  configuration_json TEXT NOT NULL DEFAULT '{}',
  reply TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','completed','failed','cancelled','interrupted')),
  error_code TEXT NOT NULL DEFAULT '',
  revision BIGINT NOT NULL DEFAULT 1,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  worker_id TEXT NOT NULL DEFAULT '',
  lease_until BIGINT NOT NULL DEFAULT 0,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  started_at BIGINT,
  finished_at BIGINT,
  UNIQUE (owner_id, request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_resident_requests_active
  ON resident_requests(thread_id) WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS idx_resident_requests_history
  ON resident_requests(thread_id, id DESC);
CREATE TABLE IF NOT EXISTS resident_cancel_requests (
  owner_id INTEGER NOT NULL REFERENCES users(id),
  request_id TEXT NOT NULL,
  thread_id TEXT NOT NULL REFERENCES resident_conversations(thread_id),
  target_turn_id TEXT NOT NULL REFERENCES resident_requests(turn_id),
  created_at BIGINT NOT NULL,
  PRIMARY KEY (owner_id, request_id)
);
UPDATE _schema_version SET version = CASE WHEN version < 51 THEN 51 ELSE version END;
