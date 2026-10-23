-- Optional experts for every entry point. Rebuilds preserve public and integer ids.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
CREATE TABLE channels_unified (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 channel_id TEXT NOT NULL UNIQUE,
 agent_id TEXT REFERENCES agents(agent_id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, name TEXT NOT NULL, config_json TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(user_id,agent_id,name)
);
INSERT INTO channels_unified SELECT * FROM channels;
DROP TABLE channels;
ALTER TABLE channels_unified RENAME TO channels;
CREATE TABLE cron_jobs_unified (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 cron_id TEXT NOT NULL UNIQUE,
 agent_id TEXT REFERENCES agents(agent_id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 schedule_spec TEXT NOT NULL, prompt TEXT NOT NULL, session_key TEXT NOT NULL, model TEXT,
 fresh_thread INTEGER NOT NULL DEFAULT 0,
 task_type TEXT NOT NULL DEFAULT 'agent' CHECK(task_type IN ('text','agent')),
 enabled INTEGER NOT NULL DEFAULT 1, last_run_at INTEGER, last_status TEXT, last_error TEXT,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 mcp_servers TEXT NOT NULL DEFAULT '[]', name TEXT NOT NULL DEFAULT ''
);
INSERT INTO cron_jobs_unified(id,cron_id,agent_id,user_id,schedule_spec,prompt,session_key,model,
 fresh_thread,task_type,enabled,last_run_at,last_status,last_error,created_at,updated_at,mcp_servers,name)
 SELECT id,cron_id,agent_id,user_id,schedule_spec,prompt,session_key,model,fresh_thread,task_type,
 enabled,last_run_at,last_status,last_error,created_at,updated_at,mcp_servers,name FROM cron_jobs;
DROP TABLE cron_jobs;
ALTER TABLE cron_jobs_unified RENAME TO cron_jobs;
COMMIT;
PRAGMA foreign_keys=ON;
ALTER TABLE cron_jobs ADD COLUMN selected_expert_id TEXT REFERENCES agents(agent_id) ON DELETE SET NULL;
UPDATE cron_jobs SET selected_expert_id=agent_id;
ALTER TABLE cron_jobs ADD COLUMN project_id TEXT REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE cron_jobs ADD COLUMN target_task_id TEXT REFERENCES project_tasks(task_id) ON DELETE SET NULL;
ALTER TABLE cron_jobs ADD COLUMN conversation_policy TEXT NOT NULL DEFAULT 'continue_target'
 CHECK(conversation_policy IN ('continue_target','per_run','per_job','task'));
CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_general_name ON channels(user_id,name) WHERE agent_id IS NULL;
-- Unified conversation resources
-- Tasks and conversations share optional expert identity and configuration history.
ALTER TABLE agents ADD COLUMN config_revision BIGINT NOT NULL DEFAULT 1;
ALTER TABLE project_tasks ADD COLUMN selected_expert_id TEXT REFERENCES agents(agent_id);
ALTER TABLE project_tasks ADD COLUMN configuration_revision BIGINT NOT NULL DEFAULT 1;
ALTER TABLE project_tasks ADD COLUMN internal_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE project_tasks ADD COLUMN action_mode TEXT NOT NULL DEFAULT 'craft'
  CHECK (action_mode IN ('ask', 'plan', 'craft'));
ALTER TABLE project_tasks ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1));
CREATE INDEX IF NOT EXISTS idx_project_tasks_global_page
  ON project_tasks(owner_id, archived, pinned DESC, updated_at DESC, task_id DESC);
CREATE INDEX IF NOT EXISTS idx_project_tasks_expert
  ON project_tasks(owner_id, selected_expert_id, archived, updated_at DESC, task_id DESC);
CREATE TABLE IF NOT EXISTS project_task_configuration_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES project_tasks(task_id),
  configuration_revision BIGINT NOT NULL,
  previous_expert_id TEXT,
  selected_expert_id TEXT,
  selected_expert_name TEXT NOT NULL DEFAULT '',
  previous_action_mode TEXT NOT NULL,
  action_mode TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  UNIQUE(task_id, configuration_revision)
);
CREATE TABLE IF NOT EXISTS project_task_branches (
  task_id TEXT PRIMARY KEY REFERENCES project_tasks(task_id),
  owner_id INTEGER NOT NULL REFERENCES users(id),
  request_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  source_task_id TEXT NOT NULL,
  source_turn_id TEXT NOT NULL,
  operation TEXT NOT NULL CHECK (operation IN ('fork', 'edit', 'regenerate')),
  continuation_prompt TEXT,
  continuation_attachments_json TEXT NOT NULL DEFAULT '[]',
  created_at BIGINT NOT NULL,
  UNIQUE(owner_id, request_id)
);
CREATE TABLE IF NOT EXISTS project_task_legacy_sources (
  task_id TEXT PRIMARY KEY REFERENCES project_tasks(task_id),
  owner_id INTEGER NOT NULL REFERENCES users(id),
  request_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  source_agent_id TEXT NOT NULL,
  source_thread_id TEXT NOT NULL,
  source_digest TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  UNIQUE(owner_id, request_id)
);
CREATE TABLE IF NOT EXISTS expert_memory_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL UNIQUE,
  owner_id INTEGER NOT NULL,
  expert_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  namespace TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','cancelled','failed','uncertain')),
  phase TEXT NOT NULL DEFAULT 'queued' CHECK (phase IN ('queued','extracting','ready','applying','done')),
  result_json TEXT NOT NULL DEFAULT '{}',
  worker_id TEXT NOT NULL DEFAULT '',
  lease_until BIGINT NOT NULL DEFAULT 0,
  error_code TEXT NOT NULL DEFAULT '',
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expert_memory_jobs_queue ON expert_memory_jobs(status,lease_until,created_at);
CREATE INDEX IF NOT EXISTS idx_expert_memory_jobs_task ON expert_memory_jobs(owner_id,task_id,created_at);
CREATE TABLE IF NOT EXISTS unified_source_routes (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 route_id TEXT NOT NULL UNIQUE,
 owner_id INTEGER NOT NULL REFERENCES users(id),
 source_kind TEXT NOT NULL,
 source_instance TEXT NOT NULL,
 source_key TEXT NOT NULL,
 task_id TEXT REFERENCES project_tasks(task_id) ON DELETE SET NULL,
 pending_task_id TEXT,
 legacy_thread_id TEXT,
 legacy_agent_id TEXT,
 state TEXT NOT NULL DEFAULT 'task',
 revision BIGINT NOT NULL DEFAULT 1,
 source_digest TEXT NOT NULL DEFAULT '',
 metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at BIGINT NOT NULL,
 updated_at BIGINT NOT NULL,
 UNIQUE(owner_id,source_kind,source_instance,source_key)
);
CREATE INDEX IF NOT EXISTS idx_unified_routes_legacy ON unified_source_routes(owner_id,legacy_thread_id);
CREATE TABLE IF NOT EXISTS unified_source_inbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 inbox_id TEXT NOT NULL UNIQUE,
 route_id TEXT NOT NULL REFERENCES unified_source_routes(route_id),
 owner_id INTEGER NOT NULL REFERENCES users(id),
 event_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'received',
 error_code TEXT NOT NULL DEFAULT '',
 task_id TEXT REFERENCES project_tasks(task_id) ON DELETE SET NULL,
 turn_id TEXT REFERENCES project_task_turns(turn_id) ON DELETE SET NULL,
 created_at BIGINT NOT NULL,
 updated_at BIGINT NOT NULL,
 UNIQUE(route_id,event_id)
);
CREATE INDEX IF NOT EXISTS idx_unified_inbox_pending ON unified_source_inbox(status,id);
CREATE TABLE IF NOT EXISTS unified_source_deliveries (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 delivery_id TEXT NOT NULL UNIQUE,
 route_id TEXT NOT NULL REFERENCES unified_source_routes(route_id),
 owner_id INTEGER NOT NULL REFERENCES users(id),
 task_id TEXT NOT NULL REFERENCES project_tasks(task_id),
 turn_id TEXT NOT NULL REFERENCES project_task_turns(turn_id),
 delivery_key TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',
 worker_id TEXT NOT NULL DEFAULT '',
 lease_until BIGINT NOT NULL DEFAULT 0,
 receipt_json TEXT NOT NULL DEFAULT '{}',
 error_code TEXT NOT NULL DEFAULT '',
 created_at BIGINT NOT NULL,
 updated_at BIGINT NOT NULL,
 UNIQUE(route_id,turn_id,delivery_key)
);
CREATE TABLE IF NOT EXISTS unified_cron_occurrences (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 occurrence_id TEXT NOT NULL UNIQUE,
 cron_id TEXT NOT NULL REFERENCES cron_jobs(cron_id) ON DELETE CASCADE,
 owner_id INTEGER NOT NULL REFERENCES users(id),
 event_key TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'received',
 task_id TEXT REFERENCES project_tasks(task_id) ON DELETE SET NULL,
 turn_id TEXT REFERENCES project_task_turns(turn_id) ON DELETE SET NULL,
 created_at BIGINT NOT NULL,
 updated_at BIGINT NOT NULL,
 UNIQUE(cron_id,event_key)
);

CREATE TABLE IF NOT EXISTS conversation_migration_batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_id TEXT NOT NULL UNIQUE,
 owner_id INTEGER NOT NULL REFERENCES users(id),
 request_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 configuration_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued',
 created_at BIGINT NOT NULL,
 updated_at BIGINT NOT NULL,
 UNIQUE(owner_id,request_id)
);
CREATE TABLE IF NOT EXISTS conversation_migration_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 item_id TEXT NOT NULL UNIQUE,
 batch_id TEXT NOT NULL REFERENCES conversation_migration_batches(batch_id),
 owner_id INTEGER NOT NULL REFERENCES users(id),
 source_agent_id TEXT NOT NULL,
 source_thread_id TEXT NOT NULL,
 route_id TEXT NOT NULL DEFAULT '',
 target_task_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',
 phase TEXT NOT NULL DEFAULT 'discovered',
 source_digest TEXT NOT NULL DEFAULT '',
 manifest_json TEXT NOT NULL DEFAULT '{}',
 error_code TEXT NOT NULL DEFAULT '',
 worker_id TEXT NOT NULL DEFAULT '',
 lease_until BIGINT NOT NULL DEFAULT 0,
 created_at BIGINT NOT NULL,
 updated_at BIGINT NOT NULL,
 UNIQUE(batch_id,source_thread_id)
);
CREATE INDEX IF NOT EXISTS idx_conversation_migrations_queue
 ON conversation_migration_items(status,lease_until,id);
CREATE INDEX IF NOT EXISTS idx_conversation_migrations_owner
 ON conversation_migration_batches(owner_id,id DESC);

CREATE TABLE IF NOT EXISTS federated_task_hosts (
 host_id TEXT PRIMARY KEY REFERENCES bridge_connections(connection_id) ON DELETE CASCADE,
 owner_id INTEGER NOT NULL REFERENCES users(id),
 peer_identity TEXT NOT NULL,
 connection_fingerprint TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS federated_task_refs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ref_id TEXT NOT NULL UNIQUE,
 owner_id INTEGER NOT NULL REFERENCES users(id),
 host_id TEXT NOT NULL REFERENCES bridge_connections(connection_id) ON DELETE CASCADE,
 peer_identity TEXT NOT NULL,
 task_id TEXT NOT NULL,
 summary_json TEXT NOT NULL,
 observed_at BIGINT NOT NULL,
 UNIQUE(owner_id,host_id,task_id)
);
CREATE INDEX IF NOT EXISTS idx_federated_tasks_owner ON federated_task_refs(owner_id,host_id,observed_at DESC);

UPDATE _schema_version SET version = 56;
