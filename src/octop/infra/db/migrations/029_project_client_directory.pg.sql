-- Store a client path separately from the server workspace directory.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS client_directory TEXT;
UPDATE _schema_version SET version = 29;
