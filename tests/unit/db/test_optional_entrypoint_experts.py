"""The unreleased schema upgrade retains entrypoint identity and explicit clears."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool


def legacy_database(tmp_path, monkeypatch, version):
    previous = tmp_path / "migrations-55"
    previous.mkdir()
    for number, path in migrate._discover("sqlite"):
        if number <= 55:
            shutil.copy2(path, previous / path.name)
    db = SqlitePool(tmp_path / "entrypoints.db")
    with monkeypatch.context() as patch:
        patch.setattr(migrate, "_MIGRATIONS_DIR", previous)
        migrate.run_migrations(db)
    if version == 56:
        source = next(
            path for number, path in migrate._discover("sqlite") if number == 56
        ).read_text()
        # Existing unreleased v56 had unified Tasks but required legacy agents
        # on channel/cron entrypoints; exercise that watermark independently.
        resources = source.split("-- Unified conversation resources\n", 1)[1]
        with db.transaction() as conn:
            for statement in migrate._split_pg_sql(resources):
                conn.execute(statement)
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)")
        conn.execute(
            "INSERT INTO agents(agent_id,user_id,name,created_at,updated_at) VALUES('expert',1,'Expert',1,2)"
        )
        conn.execute(
            "INSERT INTO channels(id,channel_id,agent_id,user_id,kind,name,config_json,enabled,created_at,updated_at) VALUES(21,'channel','expert',1,'telegram','Channel','{\"retained\":true}',0,111,222)"
        )
        conn.execute(
            "INSERT INTO cron_jobs(id,cron_id,agent_id,user_id,schedule_spec,prompt,session_key,model,fresh_thread,task_type,enabled,last_run_at,last_status,last_error,created_at,updated_at,mcp_servers,name) VALUES(34,'cron','expert',1,'0 9 * * *','Original prompt','original-session','provider/model',1,'agent',0,123,'failed','old-error',111,222,'[\"mcp-original\"]','Original name')"
        )
        if version == 56:
            conn.execute(
                "INSERT INTO unified_cron_occurrences(occurrence_id,cron_id,owner_id,event_key,payload_json,status,created_at,updated_at) VALUES('occurrence','cron',1,'original-event','{\"retained\":true}','received',111,222)"
            )
    return db


@pytest.mark.parametrize("watermark", [55, 56])
def test_optional_upgrade_preserves_rows_and_first_expert_backfill(
    tmp_path, monkeypatch, watermark
):
    db = legacy_database(tmp_path, monkeypatch, watermark)
    try:
        with db.connect() as conn:
            before_channel = dict(conn.execute("SELECT * FROM channels").fetchone())
            before_cron = dict(conn.execute("SELECT * FROM cron_jobs").fetchone())
            occurrence = (
                dict(conn.execute("SELECT * FROM unified_cron_occurrences").fetchone())
                if watermark == 56
                else None
            )
        migrate.run_migrations(db)
        migrate.run_migrations(db)
        with db.transaction() as conn:
            channel = dict(conn.execute("SELECT * FROM channels").fetchone())
            cron = dict(conn.execute("SELECT * FROM cron_jobs").fetchone())
            assert channel == before_channel
            for key, value in before_cron.items():
                assert cron[key] == value
            assert cron["selected_expert_id"] == "expert"
            assert cron["conversation_policy"] == "continue_target"
            assert cron["project_id"] is None and cron["target_task_id"] is None
            if occurrence:
                assert (
                    dict(conn.execute("SELECT * FROM unified_cron_occurrences").fetchone())
                    == occurrence
                )
            for table in ("channels", "cron_jobs"):
                agent = next(
                    row
                    for row in conn.execute(f"PRAGMA table_info({table})")
                    if row["name"] == "agent_id"
                )
                assert not agent["notnull"]
            conn.execute("UPDATE channels SET agent_id=NULL WHERE channel_id='channel'")
            conn.execute(
                "UPDATE cron_jobs SET selected_expert_id=NULL,conversation_policy='per_job' WHERE cron_id='cron'"
            )
        migrate.run_migrations(db)
        with db.connect() as conn:
            assert (
                conn.execute(
                    "SELECT selected_expert_id FROM cron_jobs WHERE cron_id='cron'"
                ).fetchone()[0]
                is None
            )
            assert (
                conn.execute("SELECT agent_id FROM channels WHERE channel_id='channel'").fetchone()[
                    0
                ]
                is None
            )
            assert (
                conn.execute(
                    "SELECT conversation_policy FROM cron_jobs WHERE cron_id='cron'"
                ).fetchone()[0]
                == "per_job"
            )
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 58
    finally:
        db.close()


def test_partial_upgrade_retains_added_configuration_and_explicit_clear(tmp_path, monkeypatch):
    db = legacy_database(tmp_path, monkeypatch, 56)
    try:
        # An interrupted/add-column-first v56 upgrade must not lose values when
        # the old NOT NULL table is subsequently rebuilt.
        with db.transaction() as conn:
            conn.execute(
                "ALTER TABLE cron_jobs ADD COLUMN selected_expert_id TEXT REFERENCES agents(agent_id) ON DELETE SET NULL"
            )
            conn.execute(
                "ALTER TABLE cron_jobs ADD COLUMN project_id TEXT REFERENCES projects(project_id) ON DELETE SET NULL"
            )
            conn.execute(
                "ALTER TABLE cron_jobs ADD COLUMN target_task_id TEXT REFERENCES project_tasks(task_id) ON DELETE SET NULL"
            )
            conn.execute(
                "ALTER TABLE cron_jobs ADD COLUMN conversation_policy TEXT NOT NULL DEFAULT 'continue_target'"
            )
            conn.execute(
                "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) VALUES('project',1,'Project',1,1)"
            )
            conn.execute(
                "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) VALUES('task',1,'Task','request',1,1)"
            )
            conn.execute(
                "UPDATE cron_jobs SET selected_expert_id=NULL,project_id='project',target_task_id='task',conversation_policy='task'"
            )
            before = dict(conn.execute("SELECT * FROM cron_jobs").fetchone())
        migrate.run_migrations(db)
        with db.connect() as conn:
            assert dict(conn.execute("SELECT * FROM cron_jobs").fetchone()) == before
            assert conn.execute("SELECT COUNT(*) FROM unified_cron_occurrences").fetchone()[0] == 1
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


@pytest.mark.parametrize("already_nullable", [False, True])
def test_channel_names_are_unique_per_owner_and_expert_after_repair(
    tmp_path, monkeypatch, already_nullable
):
    db = legacy_database(tmp_path, monkeypatch, 56)
    try:
        if already_nullable:
            # Previous v56 allowed general channels but still carried the
            # legacy cross-owner UNIQUE(agent_id,name) constraint.
            with db.connect() as conn:
                conn.execute("PRAGMA foreign_keys=OFF")
                conn.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE channels_older56 (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL UNIQUE,
                  agent_id TEXT REFERENCES agents(agent_id) ON DELETE CASCADE,
                  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  kind TEXT NOT NULL,name TEXT NOT NULL,config_json TEXT NOT NULL,
                  enabled INTEGER NOT NULL DEFAULT 1,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
                  UNIQUE(agent_id,name)
                );
                INSERT INTO channels_older56 SELECT * FROM channels;
                DROP TABLE channels;
                ALTER TABLE channels_older56 RENAME TO channels;
                COMMIT;
                """)
                conn.execute("PRAGMA foreign_keys=ON")
        migrate.run_migrations(db)
        migrate.run_migrations(db)
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO users(id,username,role,created_at) VALUES(2,'viewer','user',1)"
            )
            conn.execute("UPDATE agents SET is_shared=1 WHERE agent_id='expert'")
            for identifier, owner, expert in (
                ("shared-viewer", 2, "expert"),
                ("general-owner", 1, None),
                ("general-viewer", 2, None),
            ):
                conn.execute(
                    "INSERT INTO channels(channel_id,agent_id,user_id,kind,name,config_json,created_at,updated_at) VALUES(?,?,?,'telegram','Channel','{}',1,1)",
                    (identifier, expert, owner),
                )
        for identifier, expert in (("duplicate-expert", "expert"), ("duplicate-general", None)):
            with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
                conn.execute(
                    "INSERT INTO channels(channel_id,agent_id,user_id,kind,name,config_json,created_at,updated_at) VALUES(?,?,1,'telegram','Channel','{}',1,1)",
                    (identifier, expert),
                )
        with db.connect() as conn:
            assert (
                conn.execute("SELECT id FROM channels WHERE channel_id='channel'").fetchone()[0]
                == 21
            )
            assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 4
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


@pytest.mark.parametrize("missing", [("channels",), ("cron_jobs",), ("channels", "cron_jobs")])
def test_optional_upgrade_skips_missing_legacy_entrypoint_tables(tmp_path, monkeypatch, missing):
    db = legacy_database(tmp_path, monkeypatch, 55)
    try:
        with db.transaction() as conn:
            for table in missing:
                conn.execute(f"DROP TABLE {table}")
            before_tables = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            before_rows = {
                table: dict(conn.execute(f"SELECT * FROM {table}").fetchone())
                for table in ("channels", "cron_jobs")
                if table not in missing
            }
        migrate._ensure_optional_entrypoint_experts(db)
        migrate._ensure_optional_entrypoint_experts(db)
        with db.connect() as conn:
            assert {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            } == before_tables
            for table, before in before_rows.items():
                after = dict(conn.execute(f"SELECT * FROM {table}").fetchone())
                assert {key: after[key] for key in before} == before
                assert not next(
                    row["notnull"]
                    for row in conn.execute(f"PRAGMA table_info({table})")
                    if row["name"] == "agent_id"
                )
                if table == "cron_jobs":
                    assert after["selected_expert_id"] == "expert"
                    assert after["conversation_policy"] == "continue_target"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        db.close()
