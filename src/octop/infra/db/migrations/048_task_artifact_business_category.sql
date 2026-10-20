-- Business classification is independent from the published file's format.
ALTER TABLE project_task_artifacts ADD COLUMN business_category
    TEXT NOT NULL DEFAULT 'uncategorized'
    CHECK (business_category IN ('report', 'data', 'workflow', 'uncategorized'));

UPDATE _schema_version SET version = 48;
