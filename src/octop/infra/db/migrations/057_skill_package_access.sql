-- Existing schema 56 installations need these columns; older upstream migration
-- numbers 21/22 have already been skipped by the local watermark.
ALTER TABLE skill_packages ADD COLUMN allow_download INTEGER NOT NULL DEFAULT 1;
ALTER TABLE skill_packages ADD COLUMN shared INTEGER NOT NULL DEFAULT 0;
-- Preserve the visibility of existing global packages; new packages default private.
UPDATE skill_packages SET shared = 1;
UPDATE _schema_version SET version = 57;
