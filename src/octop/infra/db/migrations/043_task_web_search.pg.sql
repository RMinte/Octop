-- Network search is a task setting; each turn keeps its admitted snapshot.
ALTER TABLE project_tasks ADD COLUMN web_search_enabled INTEGER NOT NULL DEFAULT 1
    CHECK (web_search_enabled IN (0, 1));
UPDATE _schema_version SET version = 43;
