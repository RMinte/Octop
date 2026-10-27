"""A v57 control plane acquires engine state without changing existing tasks."""

from __future__ import annotations

import shutil

import pytest

from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool


@pytest.mark.parametrize("direct_sql", [False, True])
def test_v57_upgrade_keeps_task_identity_and_repairs_repeated_runs(
    tmp_path, monkeypatch, direct_sql
):
    old_migrations = tmp_path / "v57"
    old_migrations.mkdir()
    for version, path in migrate._discover("sqlite"):
        if version <= 57:
            shutil.copy2(path, old_migrations / path.name)
    db = SqlitePool(tmp_path / "engines.sqlite")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            migrate.run_migrations(db)
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)"
            )
            conn.execute(
                "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) VALUES('keep',1,'Keep','keep-request',1,2)"
            )
            before = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
        if direct_sql:
            with db.connect() as conn:
                conn.executescript(
                    (migrate._MIGRATIONS_DIR / "058_task_execution_engines.sql").read_text()
                )
        migrate.run_migrations(db)
        migrate.run_migrations(db)
        with db.transaction() as conn:
            after = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
            assert after.pop("execution_json") == "{}"
            assert after == before
            conn.execute(
                "UPDATE project_tasks SET execution_json=?",
                (
                    '{"engine":{"engine_id":"octop"},"sessions":{"key":{"session_id":"keep-session"}}}',
                ),
            )
        migrate.run_migrations(db)
        with db.connect() as conn:
            assert (
                "keep-session"
                in conn.execute("SELECT execution_json FROM project_tasks").fetchone()[0]
            )
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 58
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()
