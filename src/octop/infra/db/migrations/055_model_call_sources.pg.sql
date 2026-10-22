-- Product sources are the explicit box connection and all other providers.
UPDATE model_calls SET source = 'third_party' WHERE source <> 'box';
ALTER TABLE model_calls ALTER COLUMN source SET DEFAULT 'third_party';
ALTER TABLE model_calls DROP CONSTRAINT model_calls_source_check;
ALTER TABLE model_calls ADD CONSTRAINT model_calls_source_check
    CHECK (source IN ('box', 'third_party'));
UPDATE _schema_version SET version = 55;
