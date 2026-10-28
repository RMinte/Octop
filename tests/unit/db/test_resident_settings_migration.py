"""Conversation search policy upgrades keep identities and accepted execution facts."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.resident_conversations import ResidentConversationRepo


def seed_conversation(db):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at) VALUES('owner','hash','user',1)"
        )
        conn.execute(
            "INSERT INTO agents(agent_id,user_id,name,created_at,updated_at) VALUES('agent',1,'Private',1,1)"
        )
        conn.execute(
            "INSERT INTO threads(thread_id,agent_id,user_id,channel_type,session_key,created_at,last_active) VALUES('thread','agent',1,'resident','resident-session',1,1)"
        )
        conn.execute(
            "INSERT INTO resident_conversations(owner_id,agent_id,thread_id,status,created_at,updated_at) VALUES(1,'agent','thread','ready',1000,1000)"
        )
        conn.execute(
            "INSERT INTO resident_requests(request_id,turn_id,owner_id,thread_id,prompt,configuration_json,input_json,reply,status,created_at,updated_at) "
            "VALUES('accepted','turn',1,'thread','Question',?,?,'Answer','completed',1000,2000)",
            ('{"web_search_enabled":false}', '{"read_tools":false}'),
        )


@pytest.mark.parametrize("direct_sql", [False, True])
@pytest.mark.parametrize("watermark", [59, 60, 99])
def test_v59_upgrade_and_skipped_watermark_keep_saved_turns_and_settings(
    tmp_path, monkeypatch, direct_sql, watermark
):
    previous = tmp_path / "v59"
    previous.mkdir()
    for version, path in migrate._discover("sqlite"):
        if version <= 59:
            shutil.copy2(path, previous / path.name)
    db = SqlitePool(tmp_path / "resident.sqlite")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", previous)
            migrate.run_migrations(db)
        seed_conversation(db)
        with db.transaction() as conn:
            before = dict(conn.execute("SELECT * FROM resident_conversations").fetchone())
            request_before = dict(conn.execute("SELECT * FROM resident_requests").fetchone())
            assert "web_search_enabled" not in before
            conn.execute("UPDATE _schema_version SET version=?", (watermark,))
        if direct_sql:
            with db.connect() as conn:
                conn.executescript(
                    (migrate._MIGRATIONS_DIR / "060_resident_conversation_settings.sql").read_text()
                )
        migrate.run_migrations(db)
        migrate.run_migrations(db)
        with db.connect() as conn:
            after = dict(conn.execute("SELECT * FROM resident_conversations").fetchone())
            assert after.pop("web_search_enabled") == 1
            assert after.pop("configuration_revision") == 1
            assert after == before
            assert (
                dict(conn.execute("SELECT * FROM resident_requests").fetchone()) == request_before
            )
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 60
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        repo = ResidentConversationRepo(db)
        with db.transaction() as conn:
            conn.execute("UPDATE users SET id=id WHERE id=1")
            changed = repo.update_settings_in_transaction(
                conn, 1, "agent", "thread", 1, False, 3000
            )
        assert changed is not None and changed.web_search_enabled is False
        migrate.run_migrations(db)
        assert repo.get(1) == changed
        with db.connect() as conn:
            assert (
                dict(conn.execute("SELECT * FROM resident_requests").fetchone()) == request_before
            )
    finally:
        db.close()


def test_fresh_database_adds_enabled_settings_and_enforces_constraints(tmp_path):
    db = SqlitePool(tmp_path / "fresh.sqlite")
    try:
        migrate.run_migrations(db)
        seed_conversation(db)
        row = ResidentConversationRepo(db).get(1)
        assert row is not None
        assert row.web_search_enabled is True
        assert row.configuration_revision == 1
        for assignment in ("web_search_enabled=2", "configuration_revision=0"):
            with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
                conn.execute(f"UPDATE resident_conversations SET {assignment}")
        assert ResidentConversationRepo(db).get(1) == row
    finally:
        db.close()


def test_settings_migration_has_matching_dialects():
    sqlite_path = next(path for version, path in migrate._discover("sqlite") if version == 60)
    pg_path = next(path for version, path in migrate._discover("postgresql") if version == 60)
    assert migrate._split_pg_sql(sqlite_path.read_text()) == migrate._split_pg_sql(
        pg_path.read_text()
    )
