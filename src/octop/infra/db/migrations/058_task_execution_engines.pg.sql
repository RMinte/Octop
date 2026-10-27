-- Task-owned engine selection and native-session bindings. Turn snapshots use configuration_json.
ALTER TABLE project_tasks ADD COLUMN execution_json TEXT NOT NULL DEFAULT '{}';
UPDATE _schema_version SET version = 58;
