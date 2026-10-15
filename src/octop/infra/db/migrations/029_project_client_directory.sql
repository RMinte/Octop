-- Store a client path separately from the server workspace directory.
ALTER TABLE projects ADD COLUMN client_directory TEXT;
UPDATE _schema_version SET version = 29;
