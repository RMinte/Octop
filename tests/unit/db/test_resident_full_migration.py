"""Professional-instance upgrades preserve existing identities and foreign keys."""

from __future__ import annotations

import re
import shutil

import pytest

from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool


@pytest.mark.parametrize("watermark", [52, 53])
def test_professional_upgrade_preserves_private_identity_receipts_and_reports(
    tmp_path, monkeypatch, watermark
):
    previous = tmp_path / "old-migrations"
    previous.mkdir()
    for version, source in migrate._discover("sqlite"):
        if version <= 52:
            shutil.copy2(source, previous / source.name)
    pool = SqlitePool(tmp_path / "upgrade.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", previous)
            migrate.run_migrations(pool)
        with pool.transaction() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES('owner','hash','user',1)"
            )
            conn.execute(
                "INSERT INTO agents(agent_id,user_id,name,created_at,updated_at) VALUES('agent',1,'Private',1,1)"
            )
            conn.execute(
                "INSERT INTO threads(thread_id,agent_id,user_id,channel_type,session_key,created_at,last_active) VALUES('thread','agent',1,'resident','original-session',1,1)"
            )
            conn.execute(
                "INSERT INTO resident_conversations(owner_id,agent_id,thread_id,status,created_at,updated_at) VALUES(1,'agent','thread','ready',1000,1000)"
            )
            conn.execute(
                "INSERT INTO resident_requests(request_id,turn_id,owner_id,thread_id,prompt,reply,status,created_at,updated_at,day_key,timezone) VALUES('request','turn',1,'thread','Question','Answer','completed',1000,2000,'2026-10-20','UTC')"
            )
            conn.execute(
                "INSERT INTO resident_cancel_requests(owner_id,request_id,thread_id,target_turn_id,created_at) VALUES(1,'cancel','thread','turn',3000)"
            )
            conn.execute(
                "INSERT INTO resident_delegations(delegation_id,owner_id,origin_agent_id,origin_thread_id,source_turn_id,task_id,initial_turn_id,request_id,fingerprint,timezone,created_at,updated_at) VALUES('delegation',1,'agent','thread','turn','task','task-turn','delegate-request','fingerprint','UTC',1000,1000)"
            )
            conn.execute(
                "INSERT INTO resident_task_reports(report_id,message_id,delegation_id,owner_id,thread_id,task_id,task_turn_id,status,text,day_key,timezone,captured_at,created_at,updated_at) VALUES('report','report-message','delegation',1,'thread','task','task-turn','completed','Result','2026-10-20','UTC',1000,1000,1000)"
            )
            tables = (
                "agents",
                "threads",
                "resident_conversations",
                "resident_requests",
                "resident_cancel_requests",
                "resident_delegations",
                "resident_task_reports",
            )
            before = {
                table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
                for table in tables
            }
            conn.execute("UPDATE _schema_version SET version=?", (watermark,))
        migrate.run_migrations(pool)
        migrate.run_migrations(pool)
        with pool.transaction() as conn:
            for table in tables:
                after = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
                if table == "agents":
                    assert after[0].pop("config_revision") == 1
                if table == "resident_conversations":
                    assert after[0].pop("read_event_seq") == 0
                    assert after[0].pop("inbox_initialized") == 0
                    assert after[0].pop("memory_capture_enabled") == 0
                    assert after[0].pop("memory_revision") == 1
                if table == "resident_requests":
                    assert after[0].pop("input_json") == "{}"
                assert after == before[table]
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            foreign_keys = conn.execute("PRAGMA foreign_key_list(resident_requests)").fetchall()
            assert any(row["table"] == "resident_conversations" for row in foreign_keys)
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 59
            # The former primary-only CHECK must be gone, without replacing primary.
            conn.execute(
                "INSERT INTO agents(agent_id,user_id,name,created_at,updated_at) VALUES('professional',1,'Writer',1,1)"
            )
            conn.execute(
                "INSERT INTO threads(thread_id,agent_id,user_id,channel_type,session_key,created_at,last_active) VALUES('professional-thread','professional',1,'resident','professional-session',1,1)"
            )
            conn.execute(
                "INSERT INTO resident_conversations(owner_id,role,agent_id,thread_id,created_at,updated_at) VALUES(1,'profile:writer','professional','professional-thread',1000,1000)"
            )
            assert (
                conn.execute(
                    "SELECT thread_id FROM resident_conversations WHERE owner_id=1 AND role='primary'"
                ).fetchone()[0]
                == "thread"
            )
    finally:
        pool.close()


def test_professional_resource_dialects_have_matching_constraints():
    sqlite = (migrate._MIGRATIONS_DIR / "053_resident_profiles_followups.sql").read_text()
    postgres = (migrate._MIGRATIONS_DIR / "053_resident_profiles_followups.pg.sql").read_text()
    sqlite = sqlite.split("-- Additional resident resources\n", 1)[1]
    postgres = postgres.split("-- Additional resident resources\n", 1)[1]
    postgres = postgres.replace(
        "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT"
    ).replace("owner_id BIGINT", "owner_id INTEGER")
    assert re.sub(r"\s+", " ", sqlite).strip() == re.sub(r"\s+", " ", postgres).strip()


def test_existing_unreleased_53_followup_constraint_expands_without_losing_authorization(tmp_path):
    pool = SqlitePool(tmp_path / "older-53.db")
    try:
        migrate.run_migrations(pool)
        with pool.transaction() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES('owner','hash','user',1)"
            )
            conn.execute(
                "INSERT INTO agents(agent_id,user_id,name,created_at,updated_at) VALUES('agent',1,'Private',1,1)"
            )
            conn.execute(
                "INSERT INTO threads(thread_id,agent_id,user_id,channel_type,session_key,created_at,last_active) VALUES('thread','agent',1,'resident','session',1,1)"
            )
            conn.execute(
                "INSERT INTO resident_conversations(owner_id,agent_id,thread_id,status,created_at,updated_at) VALUES(1,'agent','thread','ready',1,1)"
            )
            conn.execute(
                "INSERT INTO resident_followups(followup_id,owner_id,agent_id,thread_id,request_id,fingerprint,purpose,kind,schedule,timezone,payload_json,created_at,updated_at,next_fire_at) VALUES('note',1,'agent','thread','authorize','hash','Remember this','note','interval:60','UTC','{}',1000,1000,61000)"
            )
            before = dict(conn.execute("SELECT * FROM resident_followups").fetchone())
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='resident_followups'"
            ).fetchone()[0]
            old_sql = sql.replace("'note','task_watch','task'", "'note','task_watch'")
            assert old_sql != sql
            conn.execute("CREATE TABLE retained_followups AS SELECT * FROM resident_followups")
            conn.execute("DROP TABLE resident_followups")
            conn.execute(old_sql)
            columns = ",".join(before)
            conn.execute(
                f"INSERT INTO resident_followups({columns}) SELECT {columns} FROM retained_followups"
            )
            conn.execute("DROP TABLE retained_followups")
        migrate.run_migrations(pool)
        migrate.run_migrations(pool)
        with pool.transaction() as conn:
            assert dict(conn.execute("SELECT * FROM resident_followups").fetchone()) == before
            conn.execute(
                "INSERT INTO resident_followups(followup_id,owner_id,agent_id,thread_id,request_id,fingerprint,purpose,kind,schedule,timezone,payload_json,created_at,updated_at) VALUES('scheduled-task',1,'agent','thread','authorize-task','hash','Do authorized work','task','interval:60','UTC','{}',1000,1000)"
            )
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 59
    finally:
        pool.close()
