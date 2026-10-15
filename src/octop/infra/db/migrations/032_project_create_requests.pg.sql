-- Durable, owner-scoped creation receipts for independent projects.
CREATE TABLE IF NOT EXISTS project_create_requests (
  owner_id BIGINT NOT NULL REFERENCES users(id),
  request_id TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  fingerprint TEXT NOT NULL,
  response_json TEXT NOT NULL,
  PRIMARY KEY (owner_id, request_id)
);
UPDATE _schema_version SET version = CASE WHEN version < 32 THEN 32 ELSE version END;
