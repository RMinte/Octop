-- Preserve default identities while allowing independently scoped private profiles.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
CREATE TABLE resident_conversations_v53 (
  owner_id INTEGER NOT NULL REFERENCES users(id),
  role TEXT NOT NULL DEFAULT 'primary',
  agent_id TEXT NOT NULL UNIQUE REFERENCES agents(agent_id),
  thread_id TEXT NOT NULL UNIQUE REFERENCES threads(thread_id),
  status TEXT NOT NULL DEFAULT 'initializing' CHECK(status IN ('initializing','ready','failed')),
  error_code TEXT NOT NULL DEFAULT '',
  revision BIGINT NOT NULL DEFAULT 1,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  read_event_seq BIGINT NOT NULL DEFAULT 0,
  inbox_initialized INTEGER NOT NULL DEFAULT 0,
  memory_capture_enabled INTEGER NOT NULL DEFAULT 0,
  memory_revision BIGINT NOT NULL DEFAULT 1,
  PRIMARY KEY(owner_id,role)
);
INSERT INTO resident_conversations_v53(owner_id,role,agent_id,thread_id,status,error_code,revision,created_at,updated_at)
  SELECT owner_id,role,agent_id,thread_id,status,error_code,revision,created_at,updated_at FROM resident_conversations;
DROP TABLE resident_conversations;
ALTER TABLE resident_conversations_v53 RENAME TO resident_conversations;
COMMIT;
PRAGMA foreign_keys = ON;
ALTER TABLE resident_requests ADD COLUMN input_json TEXT NOT NULL DEFAULT '{}';
-- Additional resident resources
CREATE TABLE IF NOT EXISTS resident_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL UNIQUE,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  request_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  agent_id TEXT NOT NULL UNIQUE REFERENCES agents(agent_id),
  thread_id TEXT NOT NULL UNIQUE REFERENCES resident_conversations(thread_id),
  name TEXT NOT NULL,
  template_kind TEXT NOT NULL CHECK(template_kind IN ('builtin','published')),
  template_id TEXT NOT NULL,
  template_version TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  revision BIGINT NOT NULL DEFAULT 1,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  UNIQUE(owner_id,request_id)
);
CREATE INDEX IF NOT EXISTS idx_resident_profiles_owner ON resident_profiles(owner_id,id DESC);
CREATE TABLE IF NOT EXISTS resident_notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  notification_id TEXT NOT NULL UNIQUE,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  agent_id TEXT NOT NULL,
  thread_id TEXT NOT NULL REFERENCES resident_conversations(thread_id),
  followup_id TEXT NOT NULL,
  authorization_version BIGINT NOT NULL,
  source_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  message_id TEXT NOT NULL UNIQUE,
  day_key TEXT NOT NULL,
  timezone TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','delivered','discarded')),
  archive_written_at BIGINT,
  context_written_at BIGINT,
  message_json TEXT NOT NULL DEFAULT '',
  delivery_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_resident_notifications_pending ON resident_notifications(status,id);
CREATE INDEX IF NOT EXISTS idx_resident_notifications_scope ON resident_notifications(owner_id,thread_id,created_at DESC,notification_id DESC);
CREATE INDEX IF NOT EXISTS idx_resident_notifications_days ON resident_notifications(owner_id,thread_id,day_key,created_at DESC,notification_id DESC);
CREATE TABLE IF NOT EXISTS resident_inbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  thread_id TEXT NOT NULL REFERENCES resident_conversations(thread_id),
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  message_id TEXT NOT NULL UNIQUE,
  created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resident_inbox_scope ON resident_inbox(owner_id,thread_id,id);

CREATE TABLE IF NOT EXISTS resident_followups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  followup_id TEXT NOT NULL UNIQUE,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  agent_id TEXT NOT NULL,
  thread_id TEXT NOT NULL REFERENCES resident_conversations(thread_id),
  request_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  purpose TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('note','task_watch','task')),
  schedule TEXT NOT NULL,
  timezone TEXT NOT NULL,
  subject_task_id TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL,
  capabilities_json TEXT NOT NULL DEFAULT '["conversation_notify"]',
  notification_target TEXT NOT NULL DEFAULT 'conversation' CHECK(notification_target='conversation'),
  authorization_version BIGINT NOT NULL DEFAULT 1,
  revision BIGINT NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'enabled' CHECK(status IN ('enabled','paused','cancelled','completed')),
  next_fire_at BIGINT,
  last_observed_json TEXT NOT NULL DEFAULT '',
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  UNIQUE(owner_id,request_id)
);
CREATE INDEX IF NOT EXISTS idx_resident_followups_scope
  ON resident_followups(owner_id,thread_id,id DESC);
CREATE INDEX IF NOT EXISTS idx_resident_followups_due
  ON resident_followups(status,next_fire_at,id);

UPDATE _schema_version SET version = CASE WHEN version < 53 THEN 53 ELSE version END;
