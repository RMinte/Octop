-- Persistent per-conversation search policy, snapshotted when each turn is accepted.
ALTER TABLE resident_conversations ADD COLUMN web_search_enabled INTEGER NOT NULL DEFAULT 1 CHECK (web_search_enabled IN (0,1));
ALTER TABLE resident_conversations ADD COLUMN configuration_revision BIGINT NOT NULL DEFAULT 1 CHECK (configuration_revision >= 1);
UPDATE _schema_version SET version = 60;
