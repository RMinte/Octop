-- Independent task capability selection and private canonical conversation context.
ALTER TABLE projects ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE project_task_turns ADD COLUMN reasoning TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS project_task_contexts (
  task_id TEXT PRIMARY KEY REFERENCES project_tasks(task_id),
  turn_id TEXT NOT NULL REFERENCES project_task_turns(turn_id),
  messages_json TEXT NOT NULL
);
UPDATE _schema_version SET version = 39;
