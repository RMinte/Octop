"""Existing schedules keep account approvals until explicitly authorized."""

from __future__ import annotations

import shutil

import pytest

from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.cron import CronJobRepo


@pytest.mark.parametrize("direct_sql", [False, True])
@pytest.mark.parametrize("watermark", [58, 59, 99])
def test_confirmation_upgrade_retains_existing_schedule(
    tmp_path, monkeypatch, direct_sql, watermark
):
    old_migrations = tmp_path / "v58"
    old_migrations.mkdir()
    for version, path in migrate._discover("sqlite"):
        if version <= 58:
            shutil.copy2(path, old_migrations / path.name)
    db = SqlitePool(tmp_path / "cron.sqlite")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            migrate.run_migrations(db)
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)"
            )
            conn.execute(
                "INSERT INTO cron_jobs(cron_id,user_id,schedule_spec,prompt,session_key,created_at,updated_at) VALUES('keep',1,'interval:3600','Keep prompt','cron:keep',1,2)"
            )
            before = dict(conn.execute("SELECT * FROM cron_jobs").fetchone())
            assert "skip_confirmation" not in before
            conn.execute("UPDATE _schema_version SET version=?", (watermark,))
        if direct_sql:
            with db.connect() as conn:
                conn.executescript(
                    (migrate._MIGRATIONS_DIR / "059_cron_confirmation.sql").read_text()
                )
        migrate.run_migrations(db)
        migrate.run_migrations(db)
        with db.connect() as conn:
            after = dict(conn.execute("SELECT * FROM cron_jobs").fetchone())
            assert after.pop("skip_confirmation") == 0
            assert after == before
            conn.execute("UPDATE cron_jobs SET skip_confirmation=1 WHERE cron_id='keep'")
        migrate.run_migrations(db)
        assert CronJobRepo(db).get("keep").skip_confirmation is True
        with db.connect() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 60
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


def test_confirmation_migration_has_matching_dialects():
    sqlite_path = next(path for version, path in migrate._discover("sqlite") if version == 59)
    pg_path = next(path for version, path in migrate._discover("postgresql") if version == 59)
    assert migrate._split_pg_sql(sqlite_path.read_text()) == migrate._split_pg_sql(
        pg_path.read_text()
    )
