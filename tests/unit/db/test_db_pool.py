"""tests/unit/test_db_pool.py"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


def test_db_pool_creates_file(tmp_path: Path):
    db_path = tmp_path / "octop.db"
    SqlitePool(db_path)
    assert db_path.exists()


@pytest.mark.parametrize("direct_sql", [False, True])
def test_external_import_upgrade_preserves_existing_tasks(tmp_path, monkeypatch, direct_sql):
    import shutil

    from octop.infra.db import migrate

    old_migrations = tmp_path / "v49-migrations"
    old_migrations.mkdir()
    for version, source in migrate._discover("sqlite"):
        if version <= 49:
            shutil.copy2(source, old_migrations / source.name)
    pool = SqlitePool(tmp_path / "imports-upgrade.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            run_migrations(pool)
        with pool.transaction() as conn:
            owner = conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) "
                "VALUES('import-owner','x','user',1) RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) "
                "VALUES('task_preserved',?,'Keep task','keep-request',123,456)",
                (owner,),
            )
            before = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
        if direct_sql:
            with pool.connect() as conn:
                conn.executescript(
                    (migrate._MIGRATIONS_DIR / "050_external_imports.sql").read_text("utf-8")
                )
        run_migrations(pool)
        run_migrations(pool)
        with pool.connect() as conn:
            after = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
            assert after.pop("import_source") == ""
            assert after.pop("external_session_id") == ""
            assert after.pop("selected_expert_id") is None
            assert after.pop("configuration_revision") == 1
            assert after.pop("internal_kind") == ""
            assert after.pop("action_mode") == "craft"
            assert after.pop("pinned") == 0
            assert after == before
            for table in (
                "external_import_projects",
                "external_import_sessions",
                "external_import_configurations",
            ):
                assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


def test_read_snapshot_keeps_one_view_without_blocking_wal_writer(tmp_path: Path) -> None:
    path = tmp_path / "read-snapshot.db"
    reader = SqlitePool(path)
    writer = SqlitePool(path)
    try:
        with writer.transaction() as conn:
            conn.execute("CREATE TABLE snapshot_fixture(value INTEGER NOT NULL)")
            conn.execute("INSERT INTO snapshot_fixture VALUES(1)")
        with reader.read_snapshot() as conn:
            assert conn.execute("SELECT COUNT(*) FROM snapshot_fixture").fetchone()[0] == 1
            with writer.transaction() as other:
                other.execute("INSERT INTO snapshot_fixture VALUES(2)")
            assert conn.execute("SELECT COUNT(*) FROM snapshot_fixture").fetchone()[0] == 1
        with reader.read_snapshot() as conn:
            assert conn.execute("SELECT COUNT(*) FROM snapshot_fixture").fetchone()[0] == 2
    finally:
        reader.close()
        writer.close()


class _ObservedClose:
    """Keep a broken close implementation from crashing while the assertion fails."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        entered: threading.Event,
        allowed: threading.Event,
    ) -> None:
        self.connection = connection
        self.entered = entered
        self.allowed = allowed

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

    def close(self) -> None:
        self.entered.set()
        assert self.allowed.wait(5)
        self.connection.close()


@pytest.mark.parametrize("scope", ["connect", "transaction"])
def test_pool_close_waits_for_active_connection_or_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    pool = SqlitePool(tmp_path / "close-waits.db")
    close_entered, close_allowed = threading.Event(), threading.Event()
    started, finished = threading.Event(), threading.Event()
    failures: list[BaseException] = []
    connection = pool._conn
    monkeypatch.setattr(pool, "_conn", _ObservedClose(connection, close_entered, close_allowed))

    def close() -> None:
        started.set()
        try:
            pool.close()
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=close)
    try:
        with getattr(pool, scope)() as conn:
            conn.execute("CREATE TABLE close_fixture(value INTEGER NOT NULL)")
            conn.execute("INSERT INTO close_fixture VALUES(7)")
            worker.start()
            assert started.wait(5)
            # Closing must not even reach the real connection while another
            # thread owns connect()/transaction(), including its final COMMIT.
            assert not close_entered.wait(0.1)
            assert not finished.is_set()
        assert close_entered.wait(5)
    finally:
        # The context has exited before letting sqlite3.Connection.close run,
        # even if the negative assertion exposed a regression above.
        close_allowed.set()
        if worker.ident is not None:
            worker.join(timeout=5)
        pool.close()
    assert not worker.is_alive() and finished.is_set()
    assert failures == []
    with sqlite3.connect(pool.path) as reopened:
        assert reopened.execute("SELECT value FROM close_fixture").fetchone()[0] == 7
    with pytest.raises(sqlite3.ProgrammingError, match="closed"), pool.connect() as conn:
        conn.execute("SELECT 1")


def test_pool_close_is_idempotent_and_closed_operations_raise_programming_error(
    tmp_path: Path,
) -> None:
    pool = SqlitePool(tmp_path / "closed.db")
    pool.close()
    pool.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"), pool.connect() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"), pool.transaction():
        pass
    pool.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only mode bits")
def test_db_pool_file_is_0600(tmp_path: Path):
    db_path = tmp_path / "octop.db"
    SqlitePool(db_path)
    mode = db_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_run_migrations_creates_tables(db: SqlitePool):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {r["name"] for r in rows}
    expected = {
        "projects",
        "project_tasks",
        "project_task_turns",
        "project_task_tool_calls",
        "project_task_titles",
        "project_task_artifacts",
        "project_task_attachments",
        "project_create_requests",
        "project_sync_scopes",
        "project_sync_changes",
        "users",
        "user_policies",
        "agents",
        "providers",
        "channels",
        "cron_jobs",
        "sessions",
        "threads",
        "connectors",
        "connector_oauth_states",
        "secrets",
        "audit_log",
        "_schema_version",
        "storage_backends",
        "usage_log",
        "settings",
        "voice_providers",
        "proactive_care_config",
        "care_push_records",
        "skill_packages",
        "published_experts",
        "knowledge_bases",
        "knowledge_documents",
        "sso_providers",
        "sso_login_states",
        "trajectory_events",
    }
    assert expected.issubset(names)
    assert "knowledge_base_members" not in names


def test_v20_database_receives_independent_tables_without_changing_existing_schema(
    tmp_path: Path, monkeypatch
) -> None:
    import shutil

    from octop.infra.db import migrate

    old_migrations = tmp_path / "v20-migrations"
    old_migrations.mkdir()
    for version, source in migrate._discover("sqlite"):
        if version <= 20:
            shutil.copy2(source, old_migrations / source.name)
    pool = SqlitePool(tmp_path / "upgrade.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            run_migrations(pool)
        with pool.transaction() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 20
            conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES('preserved-user','fixture','user',1)"
            )
            before = {
                tuple(row)
                for row in conn.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                )
            }
            changed_tables = {
                "agents": {"config_revision"},
                "channels": set(),
                "cron_jobs": {
                    "selected_expert_id",
                    "project_id",
                    "target_task_id",
                    "conversation_policy",
                },
            }
            before_columns = {
                table: {
                    row["name"]: tuple(row)[2:]
                    for row in conn.execute(f"PRAGMA table_info({table})")
                }
                for table in changed_tables
            }
            before_foreign_keys = {
                table: {tuple(row)[2:] for row in conn.execute(f"PRAGMA foreign_key_list({table})")}
                for table in changed_tables
            }
        run_migrations(pool)
        run_migrations(pool)
        with pool.connect() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
            assert conn.execute("SELECT username FROM users").fetchone()[0] == "preserved-user"
            after = {
                tuple(row)
                for row in conn.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                )
            }
            before_names = {name for kind, name, _ in before if kind == "table"}
            added_tables = {
                name for kind, name, _ in after if kind == "table" and name not in before_names
            }
            # v56 adds the expert watermark and deliberately rebuilds the two
            # entrypoint tables for optional experts and owner-scoped names.
            assert {
                row for row in before if not (row[0] == "table" and row[1] in changed_tables)
            } <= after
            for table, added_columns in changed_tables.items():
                columns = {
                    row["name"]: tuple(row)[2:]
                    for row in conn.execute(f"PRAGMA table_info({table})")
                }
                assert columns.keys() == before_columns[table].keys() | added_columns
                for name, metadata in before_columns[table].items():
                    expected = (
                        (metadata[0], 0, *metadata[2:])
                        if table in {"channels", "cron_jobs"} and name == "agent_id"
                        else metadata
                    )
                    assert columns[name] == expected
                assert before_foreign_keys[table] <= {
                    tuple(row)[2:] for row in conn.execute(f"PRAGMA foreign_key_list({table})")
                }
            assert columns["conversation_policy"] == ("TEXT", 1, "'continue_target'", 0)
            agent_columns = {
                row["name"]: tuple(row)[2:] for row in conn.execute("PRAGMA table_info(agents)")
            }
            assert agent_columns["config_revision"] == ("BIGINT", 1, "1", 0)
            indexes = conn.execute("PRAGMA index_list(channels)").fetchall()
            unique_columns = {
                tuple(
                    column["name"] for column in conn.execute(f'PRAGMA index_info("{row["name"]}")')
                )
                for row in indexes
                if row["unique"]
            }
            assert ("user_id", "agent_id", "name") in unique_columns
            assert ("agent_id", "name") not in unique_columns
            assert ("user_id", "name") in unique_columns
            assert any(
                row["name"] == "idx_channels_general_name" and row["partial"] for row in indexes
            )
            assert added_tables == {
                "expert_memory_jobs",
                "model_calls",
                "resident_profiles",
                "resident_followups",
                "resident_notifications",
                "resident_inbox",
                "resident_delegations",
                "resident_task_reports",
                "resident_conversations",
                "resident_requests",
                "resident_cancel_requests",
                "user_task_approval_policies",
                "projects",
                "project_tasks",
                "project_task_turns",
                "project_task_configuration_events",
                "project_task_branches",
                "project_task_legacy_sources",
                "project_task_contexts",
                "project_task_tool_calls",
                "project_task_titles",
                "project_task_artifacts",
                "project_task_attachments",
                "project_task_reviews",
                "project_task_execution_receipts",
                "project_create_requests",
                "project_sync_scopes",
                "project_sync_changes",
                "external_import_projects",
                "external_import_sessions",
                "external_import_configurations",
                "unified_source_routes",
                "unified_source_inbox",
                "unified_source_deliveries",
                "unified_cron_occurrences",
                "conversation_migration_batches",
                "conversation_migration_items",
                "federated_task_hosts",
                "federated_task_refs",
            }
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


@pytest.mark.parametrize("recorded_version", [21, 22, 26, 27, 32])
def test_retired_agentcomputer_schema_and_data_are_preserved(
    tmp_path: Path, monkeypatch, recorded_version: int
) -> None:
    import shutil

    from octop.infra.db import migrate

    old_migrations = tmp_path / "v20-migrations"
    old_migrations.mkdir()
    for version, source in migrate._discover("sqlite"):
        if version <= 20:
            shutil.copy2(source, old_migrations / source.name)
    pool = SqlitePool(tmp_path / "historical.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            run_migrations(pool)
        with pool.transaction() as conn:
            conn.execute(
                "CREATE TABLE ac_projects(project_id TEXT PRIMARY KEY, configuration TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE ac_tasks(task_id TEXT PRIMARY KEY, project_id TEXT REFERENCES ac_projects(project_id))"
            )
            conn.execute("INSERT INTO ac_projects VALUES('old-project','preserved-config')")
            conn.execute("INSERT INTO ac_tasks VALUES('old-task','old-project')")
            conn.execute("ALTER TABLE agents ADD COLUMN managed_by TEXT")
            conn.execute(
                "ALTER TABLE agents ADD COLUMN managed_project_id TEXT REFERENCES ac_projects(project_id)"
            )
            conn.execute(
                "ALTER TABLE agents ADD COLUMN managed_task_id TEXT REFERENCES ac_tasks(task_id)"
            )
            conn.execute(
                "INSERT INTO agents(agent_id,name,enabled,last_state,created_at,updated_at,managed_by,managed_project_id) VALUES('old-agent','Old agent',0,'stopped',1,1,'agentcomputer','old-project')"
            )
            conn.execute("UPDATE _schema_version SET version=?", (recorded_version,))
            before = {
                table: (
                    conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[
                        0
                    ],
                    [dict(row) for row in conn.execute(f"SELECT * FROM {table}")],
                )
                for table in ("agents", "ac_projects", "ac_tasks")
            }
        run_migrations(pool)
        run_migrations(pool)
        with pool.connect() as conn:
            for table, (sql, rows) in before.items():
                if table != "agents":
                    assert (
                        conn.execute(
                            "SELECT sql FROM sqlite_master WHERE name=?", (table,)
                        ).fetchone()[0]
                        == sql
                    )
                current = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
                assert [
                    {key: row[key] for key in previous}
                    for row, previous in zip(current, rows, strict=True)
                ] == rows
                if table == "agents":
                    assert all(row["config_revision"] == 1 for row in current)
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ac_%'"
                )
            }
            assert names == {"ac_projects", "ac_tasks"}
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


def test_high_watermark_repairs_missing_independent_tables(tmp_path: Path, monkeypatch) -> None:
    """A skipped watermark still materializes only the independent project/task tables."""
    import shutil

    from octop.infra.db import migrate

    old_migrations = tmp_path / "v20-migrations"
    old_migrations.mkdir()
    for version, source in migrate._discover("sqlite"):
        if version <= 20:
            shutil.copy2(source, old_migrations / source.name)
    pool = SqlitePool(tmp_path / "clamp.db")
    with monkeypatch.context() as patch:
        patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
        run_migrations(pool)
    with pool.connect() as conn:
        conn.execute("UPDATE _schema_version SET version = 99")
    run_migrations(pool)
    run_migrations(pool)
    with pool.connect() as conn:
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"projects", "project_tasks", "project_task_turns", "project_create_requests"} <= names
    assert not any(name.startswith("ac_") for name in names)
    pool.close()


def test_run_migrations_idempotent(db: SqlitePool):
    run_migrations(db)
    with db.connect() as conn:
        v = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        cron_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cron_jobs)").fetchall()}
        thread_cols = {r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()}
        agent_cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
        table_names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        kb_cols = {r["name"] for r in conn.execute("PRAGMA table_info(knowledge_bases)").fetchall()}
        doc_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(knowledge_documents)").fetchall()
        }
        connector_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(connectors)").fetchall()
        }
        connector_indexes = {
            r["name"] for r in conn.execute("PRAGMA index_list(connectors)").fetchall()
        }
        sso_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sso_providers)").fetchall()}
        sso_indexes = {
            r["name"] for r in conn.execute("PRAGMA index_list(sso_providers)").fetchall()
        }
    assert v == 57
    assert "login_failed_count" in cols
    assert "login_locked_until" in cols
    assert "preferences_json" in cols
    assert "permissions" in cols
    assert "workspace_root_dir" not in cols
    assert "token_quota" not in cols
    assert "user_policies" in table_names
    assert "agent_team_members" not in table_names
    assert "kind" in agent_cols
    assert {"email", "sso_provider_id", "sso_subject"}.issubset(cols)
    assert {"kind", "extra"}.issubset(sso_cols)
    assert "idx_sso_providers_kind" in sso_indexes
    assert "user_invites" in table_names
    assert {"thread_messages", "thread_history_projection", "trajectory_events"}.issubset(
        table_names
    )
    assert "task_type" in cron_cols
    assert "mcp_servers" in cron_cols
    assert "name" in cron_cols
    assert "shared" in connector_cols
    assert "idx_connectors_user_display_name" in connector_indexes
    assert {
        "model_ref",
        "reasoning_mode",
        "reasoning_effort",
        "artifacts",
        "conversation_mode",
        "pending_plan_path",
        "hitl_policy",
    }.issubset(thread_cols)
    assert {
        "color",
        "icon_name",
        "icon_url",
        "skill_package_ids",
        "published_expert_id",
        "welcome_message",
        "knowledge_base_ids",
        "mcp_servers",
    }.issubset(agent_cols)
    assert "skill_packages" in table_names
    assert "knowledge_base_id" in kb_cols
    assert {"document_id", "path", "is_dir"}.issubset(doc_cols)


def test_repair_legacy_schema_ensures_columns(tmp_path: Path) -> None:
    """DBs missing columns must be repaired on boot even if schema version is ahead."""
    db_path = tmp_path / "octop.db"
    pool = SqlitePool(db_path)
    with pool.connect() as conn:
        conn.executescript(
            (
                Path(__file__).resolve().parents[3]
                / "src/octop/infra/db/migrations/001_initial.sql"
            ).read_text()
        )
        # Simulate a schema version ahead of what the columns reflect
        conn.execute("UPDATE _schema_version SET version = 2")
    run_migrations(pool)
    with pool.connect() as conn:
        cron_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cron_jobs)").fetchall()}
    assert "task_type" in cron_cols
    assert "name" in cron_cols


def test_migration_002_idempotent_when_column_already_present(tmp_path: Path) -> None:
    """Partial upgrade: mcp_servers exists but version still 1 must not fail."""
    db_path = tmp_path / "octop.db"
    pool = SqlitePool(db_path)
    with pool.connect() as conn:
        conn.executescript(
            (
                Path(__file__).resolve().parents[3]
                / "src/octop/infra/db/migrations/001_initial.sql"
            ).read_text()
        )
        conn.execute("ALTER TABLE cron_jobs ADD COLUMN mcp_servers TEXT NOT NULL DEFAULT '[]'")
    run_migrations(pool)
    with pool.connect() as conn:
        v = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
        cron_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cron_jobs)").fetchall()}
    assert v == 57
    assert "mcp_servers" in cron_cols
    assert "skill_packages" in {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def test_migration_005_preserves_populated_users_and_constraints(tmp_path: Path) -> None:
    """A populated v4 DB keeps users and their foreign-key references at v5."""
    db_path = tmp_path / "octop.db"
    pool = SqlitePool(db_path)
    initial_migration = (
        Path(__file__).resolve().parents[3] / "src/octop/infra/db/migrations/001_initial.sql"
    )
    with pool.connect() as conn:
        conn.executescript(initial_migration.read_text())
        conn.execute("UPDATE _schema_version SET version = 4")
        conn.execute(
            """
            INSERT INTO users(
              id, username, password_hash, role, display_name, disabled, locale,
              created_at, login_failed_count, login_locked_until, preferences_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (101, "legacy-user", "legacy-hash", "admin", "Legacy User", 1, "en", 10, 2, 20, "{}"),
        )
        conn.execute(
            """
            INSERT INTO agents(agent_id, user_id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("legacy-agent", 101, "Legacy Agent", 10, 20),
        )

    run_migrations(pool)

    with pool.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (101,)).fetchone()
        agent = conn.execute(
            "SELECT agent_id, user_id, name FROM agents WHERE agent_id = ?",
            ("legacy-agent",),
        ).fetchone()
        assert user is not None
        assert dict(user) == {
            "id": 101,
            "username": "legacy-user",
            "password_hash": "legacy-hash",
            "role": "admin",
            "display_name": "Legacy User",
            "disabled": 1,
            "locale": "en",
            "created_at": 10,
            "login_failed_count": 2,
            "login_locked_until": 20,
            "preferences_json": "{}",
            "email": None,
            "sso_provider_id": None,
            "sso_subject": None,
            "permissions": "[]",
            "role_name": None,
            "avatar_icon": None,
        }
        assert dict(agent) == {
            "agent_id": "legacy-agent",
            "user_id": 101,
            "name": "Legacy Agent",
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        user_indexes = {
            row["name"]: row["partial"] for row in conn.execute("PRAGMA index_list(users)")
        }
        assert user_indexes["idx_users_email"] == 1
        assert user_indexes["idx_users_sso"] == 1
        user_foreign_keys = conn.execute("PRAGMA foreign_key_list(users)").fetchall()
        assert any(
            row["from"] == "sso_provider_id" and row["table"] == "sso_providers"
            for row in user_foreign_keys
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO agents(agent_id, user_id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("invalid-agent", 999, "Invalid Agent", 10, 20),
            )

        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at, email) VALUES (?, ?, ?, ?, ?)",
            ("passwordless", None, "user", 30, "person@example.com"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO users(username, password_hash, role, created_at, email) VALUES (?, ?, ?, ?, ?)",
                ("duplicate-email", None, "user", 30, "person@example.com"),
            )

        conn.execute(
            """
            INSERT INTO sso_providers(
              id, enabled, display_name, issuer, client_id, scopes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1, 1, "SSO", "https://issuer.example", "octop-client", "openid", 30, 30),
        )
        conn.execute(
            """
            INSERT INTO users(username, password_hash, role, created_at, sso_provider_id, sso_subject)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("sso-user", None, "user", 30, 1, "subject-1"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO users(username, password_hash, role, created_at, sso_provider_id, sso_subject)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("duplicate-sso", None, "user", 30, 1, "subject-1"),
            )


def test_stuck_version_6_without_permissions_column_is_repaired(tmp_path: Path) -> None:
    """Pre-squash version clamp can leave schema at 6 without users.permissions."""
    db_path = tmp_path / "octop.db"
    pool = SqlitePool(db_path)
    with pool.connect() as conn:
        conn.executescript(
            (
                Path(__file__).resolve().parents[3]
                / "src/octop/infra/db/migrations/001_initial.sql"
            ).read_text()
        )
        conn.execute("UPDATE _schema_version SET version = 6")
    run_migrations(pool)
    with pool.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        version = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
    assert version == 57
    assert "permissions" in cols


def test_schema_v10_without_projection_tables_is_repaired(tmp_path: Path) -> None:
    """DBs that bumped to v10 via a colliding 010 file still get projection tables."""
    db_path = tmp_path / "octop.db"
    pool = SqlitePool(db_path)
    with pool.connect() as conn:
        conn.executescript(
            (
                Path(__file__).resolve().parents[3]
                / "src/octop/infra/db/migrations/001_initial.sql"
            ).read_text()
        )
        conn.execute("UPDATE _schema_version SET version = 10")
    run_migrations(pool)
    with pool.connect() as conn:
        version = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
        table_names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        kb_cols = {r["name"] for r in conn.execute("PRAGMA table_info(knowledge_bases)").fetchall()}
        cron_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cron_jobs)").fetchall()}
    assert version == 57
    assert {"thread_messages", "thread_history_projection", "trajectory_events"}.issubset(
        table_names
    )
    assert "max_documents" in kb_cols
    assert "name" in cron_cols


def test_ahead_of_max_schema_version_clamps_to_max(tmp_path: Path) -> None:
    """A DB whose watermark is ahead of discovered migrations clamps on boot.

    When the recorded version equals an older max (here 8) and a newer migration
    (9) exists, ``run_migrations`` applies the gap instead of clamping.
    """
    db_path = tmp_path / "octop.db"
    pool = SqlitePool(db_path)
    with pool.connect() as conn:
        conn.executescript(
            (
                Path(__file__).resolve().parents[3]
                / "src/octop/infra/db/migrations/001_initial.sql"
            ).read_text()
        )
        conn.execute("UPDATE _schema_version SET version = 8")
    run_migrations(pool)
    with pool.connect() as conn:
        version = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
        pkg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(skill_packages)").fetchall()}
        pub_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(published_experts)").fetchall()
        }
        invite_tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert version == 57
    assert "skill_package_id" in pkg_cols
    assert "published_expert_id" in pub_cols
    assert "user_invites" in invite_tables


def test_current_watermark_repairs_pre_v13_connectors_schema(tmp_path: Path) -> None:
    """A folded v13 schema change is repaired even when migration 013 is skipped."""
    pool = SqlitePool(tmp_path / "octop.db")
    with pool.connect() as conn:
        conn.executescript(
            (
                Path(__file__).resolve().parents[3]
                / "src/octop/infra/db/migrations/001_initial.sql"
            ).read_text()
        )
        conn.execute("UPDATE _schema_version SET version = 13")
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) "
            "VALUES ('owner', 'hash', 'admin', 1)"
        )
        user_id = conn.execute("SELECT id FROM users WHERE username = 'owner'").fetchone()[0]
        conn.execute(
            "INSERT INTO connectors("
            "instance_id, user_id, kind, display_name, mcp_server_name, created_at, updated_at"
            ") VALUES ('instance-1', ?, 'github', 'GitHub', 'connector_github_1', 1, 1)",
            (user_id,),
        )

    run_migrations(pool)

    with pool.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(connectors)").fetchall()}
        user_id = conn.execute("SELECT id FROM users WHERE username = 'owner'").fetchone()[0]
        conn.execute(
            "INSERT INTO connectors("
            "instance_id, user_id, kind, display_name, mcp_server_name, created_at, updated_at"
            ") VALUES ('instance-2', ?, 'github', 'GitHub Work', 'connector_github_2', 1, 1)",
            (user_id,),
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM connectors WHERE user_id = ? AND kind = 'github'",
            (user_id,),
        ).fetchone()[0]

    assert "shared" in columns
    assert count == 2
    pool.close()


def test_foreign_keys_enabled(db: SqlitePool):
    with db.connect() as conn:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1


def test_pre_squash_schema_version_clamped_and_knowledge_tables_filled(
    tmp_path: Path,
) -> None:
    """Develop DBs that applied split 005–009 must clamp to consolidated v5."""
    db_path = tmp_path / "octop.db"
    pool = SqlitePool(db_path)
    with pool.connect() as conn:
        conn.executescript(
            (
                Path(__file__).resolve().parents[3]
                / "src/octop/infra/db/migrations/001_initial.sql"
            ).read_text()
        )
        # Simulate a develop DB whose recorded version is ahead of the
        # consolidated max, so reconcile clamps and repairs knowledge tables.
        conn.execute("UPDATE _schema_version SET version = 13")
        conn.execute("ALTER TABLE agents ADD COLUMN is_shared INTEGER NOT NULL DEFAULT 0")

    run_migrations(pool)

    with pool.connect() as conn:
        version = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    assert version == 57
    assert "permissions" in user_cols
    assert {
        "published_experts",
        "sso_providers",
        "sso_login_states",
        "knowledge_bases",
        "knowledge_documents",
    }.issubset(tables)
    kb_cols = {r["name"] for r in conn.execute("PRAGMA table_info(knowledge_bases)").fetchall()}
    pkg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(skill_packages)").fetchall()}
    pub_cols = {r["name"] for r in conn.execute("PRAGMA table_info(published_experts)").fetchall()}
    assert "knowledge_base_id" in kb_cols
    assert "skill_package_id" in pkg_cols
    assert "published_expert_id" in pub_cols


def test_transaction_rolls_back_on_exception(db: SqlitePool):
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, ?, 0)",
            ("a", "h", "user"),
        )
        raise RuntimeError("boom")
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert n == 0


def test_v7_sql_files_declare_identity_rebuild() -> None:
    """The numbered v7 pair must contain the table rebuild, not only ADD COLUMN."""
    root = Path(__file__).resolve().parents[3] / "src/octop/infra/db/migrations"
    sqlite_sql = (root / "007_resource_identity_and_profile.sql").read_text(encoding="utf-8")
    pg_sql = (root / "007_resource_identity_and_profile.pg.sql").read_text(encoding="utf-8")
    for sql in (sqlite_sql, pg_sql):
        for token in (
            "knowledge_base_id",
            "document_id",
            "skill_package_id",
            "published_expert_id",
            "ALTER TABLE knowledge_bases RENAME",
            "ALTER TABLE skill_packages RENAME",
            "ALTER TABLE published_experts RENAME",
            "DROP TABLE IF EXISTS knowledge_base_members",
            "ADD COLUMN",
            "welcome_message",
            "artifacts",
        ):
            assert token in sql


def test_v7_sqlite_sql_upgrades_legacy_text_pks(tmp_path: Path) -> None:
    """A clean v6-shaped DB can execute 007.sql without going through Python helpers."""
    db_path = tmp_path / "octop.db"
    pool = SqlitePool(db_path)
    migrations = Path(__file__).resolve().parents[3] / "src/octop/infra/db/migrations"
    with pool.connect() as conn:
        conn.executescript((migrations / "001_initial.sql").read_text(encoding="utf-8"))
        conn.executescript(
            """
            CREATE TABLE skill_packages (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              created_by TEXT NOT NULL,
              skill_count INTEGER NOT NULL DEFAULT 0,
              icon_name TEXT NOT NULL DEFAULT '',
              icon_url TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE published_experts (
              id TEXT PRIMARY KEY,
              slug TEXT NOT NULL,
              name TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              created_by TEXT NOT NULL,
              source_agent_id TEXT,
              icon_name TEXT NOT NULL DEFAULT '',
              color TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            CREATE TABLE knowledge_bases (
              id TEXT PRIMARY KEY,
              owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              default_open INTEGER NOT NULL DEFAULT 0,
              shared INTEGER NOT NULL DEFAULT 0,
              icon_name TEXT NOT NULL DEFAULT '',
              embedding_model TEXT NOT NULL DEFAULT '',
              embedding_dim INTEGER NOT NULL DEFAULT 0,
              doc_count INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            CREATE TABLE knowledge_base_members (
              kb_id TEXT NOT NULL,
              user_id INTEGER NOT NULL,
              role TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              PRIMARY KEY (kb_id, user_id)
            );
            CREATE TABLE knowledge_documents (
              id TEXT PRIMARY KEY,
              kb_id TEXT NOT NULL,
              filename TEXT NOT NULL,
              content_type TEXT NOT NULL,
              byte_size INTEGER NOT NULL,
              content_hash TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'pending',
              error_message TEXT NOT NULL DEFAULT '',
              chunk_count INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, ?, 1)",
            ("owner", "h", "user"),
        )
        user_id = conn.execute("SELECT id FROM users WHERE username = 'owner'").fetchone()[0]
        conn.execute(
            "INSERT INTO skill_packages(id, name, created_by, created_at, updated_at) "
            "VALUES ('pkg1', 'Pack', 'owner', '1', '1')"
        )
        conn.execute(
            "INSERT INTO published_experts(id, slug, name, created_by, created_at, updated_at) "
            "VALUES ('pub1', 'slug', 'Expert', 'owner', 1, 1)"
        )
        conn.execute(
            "INSERT INTO knowledge_bases("
            "id, owner_user_id, name, embedding_model, embedding_dim, "
            "doc_count, created_at, updated_at) VALUES (?, ?, 'Docs', '', 0, 1, 1, 1)",
            ("kb1", user_id),
        )
        conn.execute(
            "INSERT INTO knowledge_base_members(kb_id, user_id, role, created_at) "
            "VALUES ('kb1', ?, 'owner', 1)",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO knowledge_documents("
            "id, kb_id, filename, content_type, byte_size, created_at, updated_at) "
            "VALUES ('doc1', 'kb1', 'a.md', 'text/markdown', 1, 1, 1)"
        )
        conn.executescript((migrations / "007_resource_identity_and_profile.sql").read_text())

    with pool.connect() as conn:
        version = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
        pkg = conn.execute("SELECT * FROM skill_packages").fetchone()
        pub = conn.execute("SELECT * FROM published_experts").fetchone()
        kb = conn.execute("SELECT * FROM knowledge_bases").fetchone()
        doc = conn.execute("SELECT * FROM knowledge_documents").fetchone()
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        agent_cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
        thread_cols = {r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()}
    assert version == 7
    assert "knowledge_base_members" not in tables
    assert "welcome_message" in agent_cols
    assert "artifacts" in thread_cols
    assert pkg["skill_package_id"] == "pkg1"
    assert pub["published_expert_id"] == "pub1"
    assert kb["knowledge_base_id"] == "kb1"
    assert doc["document_id"] == "doc1"
    assert doc["path"] == "a.md"
    assert doc["filename"] == "a.md"


def test_v14_to_v15_adds_sso_provider_kind_without_rebuilding(tmp_path: Path) -> None:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    with pool.connect() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_sso_providers_kind")
        conn.execute("ALTER TABLE sso_providers DROP COLUMN extra")
        conn.execute("ALTER TABLE sso_providers DROP COLUMN kind")
        conn.execute("UPDATE _schema_version SET version = 14")
        conn.execute(
            """
            INSERT INTO sso_providers(
              enabled, display_name, issuer, client_id, scopes, created_at, updated_at
            ) VALUES (1, 'Company', 'https://issuer.example', 'client', 'openid', 1, 1)
            """
        )
        provider_id = conn.execute("SELECT id FROM sso_providers").fetchone()[0]
        conn.execute(
            """
            INSERT INTO users(username, password_hash, role, created_at, sso_provider_id, sso_subject)
            VALUES ('sso-admin', 'x', 'admin', 1, ?, 'sub-1')
            """,
            (provider_id,),
        )
    run_migrations(pool)
    run_migrations(pool)
    with pool.connect() as conn:
        version = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
        row = conn.execute("SELECT id, kind, extra FROM sso_providers").fetchone()
        indexes = {r["name"] for r in conn.execute("PRAGMA index_list(sso_providers)").fetchall()}
        bound = conn.execute(
            "SELECT sso_provider_id FROM users WHERE username = 'sso-admin'"
        ).fetchone()[0]
    assert version == 57
    assert int(row["id"]) == int(provider_id)
    assert row["kind"] == "oidc"
    assert row["extra"] == "{}"
    assert "idx_sso_providers_kind" in indexes
    assert int(bound) == int(provider_id)


def _scratch(pool: SqlitePool) -> None:
    with pool.connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS scratch (k TEXT PRIMARY KEY)")


def _competitor(path: Path, *, timeout_ms: int) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
    return conn


def test_transaction_takes_the_write_lock_up_front(db: SqlitePool, tmp_path: Path) -> None:
    """A competing writer must not be able to commit in the middle of our transaction.

    With a deferred ``BEGIN`` the write lock is only taken at the first write, so a second
    connection (``octop run --workers N``, or a CLI run against a running server) commits in
    between; our own write is then rejected with SQLITE_BUSY_SNAPSHOT, which the 5s busy
    timeout cannot wait out.
    """
    _scratch(db)
    competitor = _competitor(tmp_path / "octop.db", timeout_ms=100)
    try:
        with db.transaction() as conn:
            conn.execute("SELECT COUNT(*) FROM scratch")  # read first: the trap
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("BEGIN IMMEDIATE")
    finally:
        competitor.close()


def test_transaction_completes_while_a_competing_writer_waits(
    db: SqlitePool, tmp_path: Path
) -> None:
    """A read-then-write transaction must survive a competing writer instead of aborting."""
    _scratch(db)
    competitor = _competitor(tmp_path / "octop.db", timeout_ms=5000)
    failures: list[Exception] = []

    def compete() -> None:
        try:
            competitor.execute("BEGIN IMMEDIATE")
            competitor.execute("INSERT INTO scratch VALUES('competitor')")
            competitor.execute("COMMIT")
        except Exception as exc:  # pragma: no cover - only reached on a regression
            failures.append(exc)

    with db.transaction() as conn:
        conn.execute("SELECT COUNT(*) FROM scratch")
        thread = threading.Thread(target=compete)
        thread.start()
        time.sleep(0.2)  # the competitor commits here unless our transaction holds the lock
        conn.execute("INSERT INTO scratch VALUES('ours')")
    thread.join(timeout=5)

    assert failures == []
    with db.connect() as conn:
        keys = {row["k"] for row in conn.execute("SELECT k FROM scratch").fetchall()}
    assert keys == {"competitor", "ours"}
    competitor.close()


@pytest.mark.parametrize("recorded_version", [28, 29])
def test_project_client_directory_upgrade_preserves_existing_project(
    tmp_path, monkeypatch, recorded_version
):
    import shutil

    from octop.infra.db import migrate

    old_migrations = tmp_path / "v28-migrations"
    old_migrations.mkdir()
    for version, source in migrate._discover("sqlite"):
        if version <= 28:
            shutil.copy2(source, old_migrations / source.name)
    pool = SqlitePool(tmp_path / "v28-project.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            run_migrations(pool)
        with pool.transaction() as conn:
            owner = conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES('owner','x','user',1) RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO projects(project_id,owner_id,name,directory,instructions,revision,created_at,updated_at) VALUES(?,?,?,?,?,3,123,456)",
                (
                    "project-before-upgrade",
                    owner,
                    "Existing project",
                    "server-directory-token",
                    "Keep instructions",
                ),
            )
            before = dict(conn.execute("SELECT * FROM projects").fetchone())
            assert "client_directory" not in before
            conn.execute("UPDATE _schema_version SET version=?", (recorded_version,))
        run_migrations(pool)
        run_migrations(pool)
        with pool.connect() as conn:
            after = dict(conn.execute("SELECT * FROM projects").fetchone())
            assert after.pop("client_directory") is None
            assert after.pop("capabilities_json") == "{}"
            assert after.pop("archived") == 0
            assert after == before
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


@pytest.mark.parametrize("previous_schema", [32, 33, 34])
@pytest.mark.parametrize("turn_status", ["queued", "running", "completed", "failed", "cancelled"])
def test_task_list_sync_upgrade_preserves_existing_conversations(
    tmp_path, monkeypatch, previous_schema, turn_status
):
    import shutil

    from octop.infra.db import migrate

    old_migrations = tmp_path / f"v{previous_schema}-migrations"
    old_migrations.mkdir()
    for version, source in migrate._discover("sqlite"):
        if version <= previous_schema:
            if version == 34:
                # The earlier local v34 added archive metadata, without task status.
                sql = source.read_text(encoding="utf-8").split(
                    "ALTER TABLE project_tasks ADD COLUMN status", 1
                )[0]
                (old_migrations / source.name).write_text(
                    sql + "UPDATE _schema_version SET version=34;\n", encoding="utf-8"
                )
            else:
                shutil.copy2(source, old_migrations / source.name)
    pool = SqlitePool(tmp_path / f"v{previous_schema}-tasks.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            run_migrations(pool)
        with pool.transaction() as conn:
            owner = conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES('sync-owner','x','user',1) RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) VALUES('task_old',?,'Preserved','old-request',123,456)",
                (owner,),
            )
            conn.execute(
                "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,reply,status,configuration_json,model_ref,created_at,updated_at) "
                "VALUES('turn_old','task_old','old-request','Keep prompt','Keep reply',?,'{}','fixture/model',123,456)",
                (turn_status,),
            )
            if turn_status in {"queued", "running"}:
                conn.execute("UPDATE project_tasks SET active_turn_id='turn_old'")
            before_task = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
            before_turn = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
        run_migrations(pool)
        run_migrations(pool)
        with pool.connect() as conn:
            after_task = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
            if "list_revision" not in before_task:
                assert after_task.pop("list_revision") == 1
            if "archived" not in before_task:
                assert after_task.pop("archived") == 0
            assert after_task.pop("title_source") == "manual"
            assert after_task.pop("title_revision") == 1
            assert conn.execute("SELECT COUNT(*) FROM project_task_titles").fetchone()[0] == 0
            assert after_task.pop("status") == turn_status
            assert "idx_project_tasks_archive_page" in {
                r["name"] for r in conn.execute("PRAGMA index_list(project_tasks)")
            }
            assert after_task.pop("wait_kind") == ""
            assert after_task.pop("approval_mode") == "ask_for_approval"
            assert after_task.pop("approval_revision") == 1
            assert after_task.pop("approval_updated_at") == 0
            assert after_task.pop("approval_requests_json") == "[]"
            assert after_task.pop("web_search_enabled") == 1
            assert after_task.pop("import_source") == ""
            assert after_task.pop("external_session_id") == ""
            assert after_task.pop("selected_expert_id") is None
            assert after_task.pop("configuration_revision") == 1
            assert after_task.pop("internal_kind") == ""
            assert after_task.pop("action_mode") == "craft"
            assert after_task.pop("pinned") == 0
            assert after_task == before_task
            after_turn = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
            assert after_turn.pop("reasoning") == ""
            assert after_turn.pop("content_blocks_json") == "[]"
            assert after_turn.pop("content_revision") == 0
            assert after_turn.pop("checkpoint_id") == ""
            assert after_turn.pop("pause_json") == ""
            assert after_turn.pop("interactions_json") == "[]"
            assert after_turn.pop("interaction_revision") == 0
            assert after_turn.pop("pause_requested") == 0
            assert after_turn.pop("wait_kind") == ""
            assert after_turn.pop("requests_json") == "[]"
            assert after_turn.pop("started_at") is None
            assert after_turn.pop("finished_at") is None
            assert after_turn.pop("recovery_json") == ""
            assert after_turn == before_turn
            assert conn.execute("SELECT COUNT(*) FROM project_sync_changes").fetchone()[0] == 0
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


@pytest.mark.parametrize("direct_sql", [False, True])
def test_v35_title_upgrade_preserves_existing_titles(tmp_path, monkeypatch, direct_sql):
    import shutil

    from octop.infra.db import migrate

    old_migrations = tmp_path / "v35-migrations"
    old_migrations.mkdir()
    for version, source in migrate._discover("sqlite"):
        if version <= 35:
            shutil.copy2(source, old_migrations / source.name)
    pool = SqlitePool(tmp_path / "v35-title-upgrade.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            run_migrations(pool)
        with pool.transaction() as conn:
            owner = conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) "
                "VALUES('title-owner','x','user',1) RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) "
                "VALUES('task_existing',?,'User chosen title','existing-request',123,456)",
                (owner,),
            )
            before = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
        if direct_sql:
            with pool.connect() as conn:
                conn.executescript(
                    (migrate._MIGRATIONS_DIR / "036_task_titles.sql").read_text(encoding="utf-8")
                )
        run_migrations(pool)
        run_migrations(pool)
        with pool.connect() as conn:
            after = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
            assert after.pop("title_source") == "manual"
            assert after.pop("title_revision") == 1
            assert after.pop("wait_kind") == ""
            assert after.pop("approval_mode") == "ask_for_approval"
            assert after.pop("approval_revision") == 1
            assert after.pop("approval_updated_at") == 0
            assert after.pop("approval_requests_json") == "[]"
            assert after.pop("web_search_enabled") == 1
            assert after.pop("import_source") == ""
            assert after.pop("external_session_id") == ""
            assert after.pop("selected_expert_id") is None
            assert after.pop("configuration_revision") == 1
            assert after.pop("internal_kind") == ""
            assert after.pop("action_mode") == "craft"
            assert after.pop("pinned") == 0
            assert after == before
            assert conn.execute("SELECT COUNT(*) FROM project_task_titles").fetchone()[0] == 0
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


@pytest.mark.parametrize("direct_sql", [False, True])
def test_v37_title_retry_upgrade_preserves_job_counters(tmp_path, monkeypatch, direct_sql):
    import shutil

    from octop.infra.db import migrate

    old_migrations = tmp_path / "v37-migrations"
    old_migrations.mkdir()
    for version, source in migrate._discover("sqlite"):
        if version <= 37:
            shutil.copy2(source, old_migrations / source.name)
    pool = SqlitePool(tmp_path / "v37-title-retry.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            run_migrations(pool)
        with pool.transaction() as conn:
            owner = conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) "
                "VALUES('retry-owner','x','user',1) RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) "
                "VALUES('task_retry',?,'Original title','retry-request',123,456)",
                (owner,),
            )
            conn.execute(
                "INSERT INTO project_task_titles(task_id,status,worker_id,attempts,lease_until,input_tokens,output_tokens) "
                "VALUES('task_retry','failed','old-worker',2,999,11,7)"
            )
            before = dict(conn.execute("SELECT * FROM project_task_titles").fetchone())
        if direct_sql:
            with pool.connect() as conn:
                conn.executescript(
                    (migrate._MIGRATIONS_DIR / "038_task_title_open.sql").read_text(
                        encoding="utf-8"
                    )
                )
        run_migrations(pool)
        run_migrations(pool)
        with pool.connect() as conn:
            after = dict(conn.execute("SELECT * FROM project_task_titles").fetchone())
            assert after.pop("attempt_limit") == 2
            assert after == before
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


def test_recorded_v39_repairs_reasoning_column_and_preserves_existing_turn(db):
    run_migrations(db)
    with db.transaction() as conn:
        owner = conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at) VALUES('reasoning-owner','x','user',1) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) VALUES('reasoning-task',?,'Preserved','reasoning-request',123,456)",
            (owner,),
        )
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,reply,status,configuration_json,model_ref,created_at,updated_at) "
            "VALUES('reasoning-turn','reasoning-task','reasoning-request','Keep prompt','Keep reply','completed','{}','fixture/model',123,456)"
        )
        conn.execute("ALTER TABLE project_task_turns DROP COLUMN reasoning")
        before = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
    run_migrations(db)
    run_migrations(db)
    with db.transaction() as conn:
        after = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
        assert after.pop("reasoning") == ""
        assert after == before
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
        conn.execute("UPDATE project_task_turns SET reasoning='Saved provider reasoning'")
    run_migrations(db)
    with db.connect() as conn:
        assert (
            conn.execute("SELECT reasoning FROM project_task_turns").fetchone()[0]
            == "Saved provider reasoning"
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_recorded_v40_adds_content_columns_without_rewriting_turns(db):
    from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRepo

    with db.transaction() as conn:
        owner = conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at) VALUES('content-owner','x','user',1) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) VALUES('content-task',?,'Preserved','content-request',123,456)",
            (owner,),
        )
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,reply,reasoning,status,configuration_json,model_ref,created_at,updated_at) "
            "VALUES('content-turn','content-task','content-request','Keep prompt','Keep reply','Keep thinking','completed','{}','fixture/model',123,456)"
        )
        conn.execute("ALTER TABLE project_task_turns DROP COLUMN content_blocks_json")
        conn.execute("ALTER TABLE project_task_turns DROP COLUMN content_revision")
        before = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
    run_migrations(db)
    run_migrations(db)
    with db.connect() as conn:
        after = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
        assert after.pop("content_blocks_json") == "[]"
        assert after.pop("content_revision") == 0
        assert after == before
    assert ProjectTaskTurnRepo(db).get("content-turn").reply == "Keep reply"


def test_project_archive_upgrade_preserves_projects_and_task_archive_state(tmp_path, monkeypatch):
    import shutil

    from octop.infra.db import migrate

    old_migrations = tmp_path / "v39-projects"
    old_migrations.mkdir()
    for version, source in migrate._discover("sqlite"):
        if version <= 39:
            shutil.copy2(source, old_migrations / source.name)
    pool = SqlitePool(tmp_path / "project-archive.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            run_migrations(pool)
        with pool.transaction() as conn:
            owner = conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES('archive-owner','x','user',1) RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO projects(project_id,owner_id,name,instructions,revision,created_at,updated_at) VALUES('project_old',?,'Keep project','Keep instructions',3,123,456)",
                (owner,),
            )
            conn.execute(
                "INSERT INTO project_tasks(task_id,owner_id,project_id,title,request_id,archived,created_at,updated_at) VALUES('task_old',?,'project_old','Keep archived task','old-request',1,123,456)",
                (owner,),
            )
            before_project = dict(conn.execute("SELECT * FROM projects").fetchone())
            before_task = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
        run_migrations(pool)
        with pool.transaction() as conn:
            project = dict(conn.execute("SELECT * FROM projects").fetchone())
            assert project.pop("archived") == 0 and project == before_project
            conn.execute("UPDATE projects SET archived=1")
        run_migrations(pool)
        with pool.connect() as conn:
            assert conn.execute("SELECT archived FROM projects").fetchone()[0] == 1
            after_task = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
            assert after_task.pop("wait_kind") == ""
            assert after_task.pop("approval_mode") == "ask_for_approval"
            assert after_task.pop("approval_revision") == 1
            assert after_task.pop("approval_updated_at") == 0
            assert after_task.pop("approval_requests_json") == "[]"
            assert after_task.pop("web_search_enabled") == 1
            assert after_task.pop("import_source") == ""
            assert after_task.pop("external_session_id") == ""
            assert after_task.pop("selected_expert_id") is None
            assert after_task.pop("configuration_revision") == 1
            assert after_task.pop("internal_kind") == ""
            assert after_task.pop("action_mode") == "craft"
            assert after_task.pop("pinned") == 0
            assert after_task == before_task
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


@pytest.mark.parametrize(
    "missing,default",
    [
        ("checkpoint_id", ""),
        ("pause_json", ""),
        ("interactions_json", "[]"),
        ("interaction_revision", 0),
        ("pause_requested", 0),
        ("wait_kind", ""),
        ("requests_json", "[]"),
    ],
)
def test_checkpoint_columns_repair_without_rewriting_saved_turn(db, missing, default):
    with db.transaction() as conn:
        owner = conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at) "
            "VALUES('checkpoint-owner','x','user',1) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) "
            "VALUES('checkpoint-task',?,'Keep','checkpoint-request',123,456)",
            (owner,),
        )
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,reply,status,"
            "configuration_json,model_ref,created_at,updated_at,checkpoint_id,pause_json) "
            "VALUES('checkpoint-turn','checkpoint-task','checkpoint-request','Work','Keep',"
            "'running','{}','provider/model',123,456,'native-checkpoint','{\"calls\":[]}')"
        )
        conn.execute(f"ALTER TABLE project_task_turns DROP COLUMN {missing}")
        before = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
    run_migrations(db)
    run_migrations(db)
    with db.connect() as conn:
        after = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
        assert after.pop(missing) == default
        assert after == before
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57


@pytest.mark.parametrize(
    "missing,default",
    [
        ("approval_mode", "ask_for_approval"),
        ("approval_revision", 1),
        ("approval_updated_at", 0),
        ("approval_requests_json", "[]"),
        ("web_search_enabled", 1),
    ],
)
def test_task_approval_columns_repair_preserves_task(db, missing, default):
    with db.transaction() as conn:
        owner = conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at) "
            "VALUES('approval-owner','x','user',1) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) "
            "VALUES('approval-task',?,'Keep','approval-request',123,456)",
            (owner,),
        )
        conn.execute(f"ALTER TABLE project_tasks DROP COLUMN {missing}")
        if missing == "web_search_enabled":
            conn.execute("UPDATE _schema_version SET version=42")
        before = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
    run_migrations(db)
    run_migrations(db)
    with db.connect() as conn:
        after = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
        assert after.pop(missing) == default
        assert after == before
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
    if missing == "web_search_enabled":
        with db.transaction() as conn:
            conn.execute("UPDATE project_tasks SET web_search_enabled=0")
        run_migrations(db)
        with db.connect() as conn:
            assert conn.execute("SELECT web_search_enabled FROM project_tasks").fetchone()[0] == 0


def test_task_review_schema_repaired_at_current_watermark(db):
    with db.transaction() as conn:
        conn.execute("DROP TABLE project_task_reviews")
        conn.execute("ALTER TABLE project_task_tool_calls DROP COLUMN approval_context_json")
    run_migrations(db)
    run_migrations(db)
    with db.connect() as conn:
        assert "approval_context_json" in {
            row["name"] for row in conn.execute("PRAGMA table_info(project_task_tool_calls)")
        }
        assert "idx_project_task_reviews_queue" in {
            row["name"] for row in conn.execute("PRAGMA index_list(project_task_reviews)")
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_task_image_schema_repaired_at_current_watermark(db):
    with db.transaction() as conn:
        conn.execute("DROP TABLE project_task_attachments")
    run_migrations(db)
    run_migrations(db)
    with db.connect() as conn:
        assert {
            "idx_project_task_attachments_task",
            "idx_project_task_attachments_pending",
        } <= {row["name"] for row in conn.execute("PRAGMA index_list(project_task_attachments)")}
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v46_artifact_library_index_upgrade_preserves_published_bytes(db):
    with db.transaction() as conn:
        conn.execute("DROP INDEX idx_task_artifacts_created")
        conn.execute("DROP INDEX idx_task_artifacts_task_created")
        conn.execute("UPDATE _schema_version SET version=46")
        owner = conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at) "
            "VALUES('library-owner','x','user',1) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) "
            "VALUES('library-task',?,'Keep','library-request',123,456)",
            (owner,),
        )
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
            "configuration_json,model_ref,created_at,updated_at) "
            "VALUES('library-turn','library-task','library-request','Work','completed','{}','',123,456)"
        )
        conn.execute(
            "INSERT INTO project_task_artifacts(artifact_id,task_id,turn_id,tool_call_id,"
            "request_hash,name,media_type,byte_count,sha256,source_path,content,created_at) "
            "VALUES('library-artifact','library-task','library-turn','call','hash','saved.pdf',"
            "'application/pdf',3,'digest','output/saved.pdf',?,123)",
            (b"\x00\xffx",),
        )
        before = dict(conn.execute("SELECT * FROM project_task_artifacts").fetchone())
    run_migrations(db)
    run_migrations(db)
    with db.connect() as conn:
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
        assert dict(conn.execute("SELECT * FROM project_task_artifacts").fetchone()) == before
        for index, expected in (
            ("idx_task_artifacts_created", [("created_at", 1), ("artifact_id", 1)]),
            (
                "idx_task_artifacts_task_created",
                [("task_id", 0), ("created_at", 1), ("artifact_id", 1)],
            ),
        ):
            columns = conn.execute(f"PRAGMA index_xinfo({index})").fetchall()
            assert [(row["name"], row["desc"]) for row in columns if row["key"]] == expected
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "recorded_version,apply_numbered_sql", [(47, False), (47, True), (48, False), (99, False)]
)
def test_artifact_business_category_upgrade_preserves_historical_snapshot(
    tmp_path, monkeypatch, recorded_version, apply_numbered_sql
):
    import shutil

    from octop.infra.db import migrate

    old_migrations = tmp_path / "v47-migrations"
    old_migrations.mkdir()
    for source in migrate._MIGRATIONS_DIR.glob("*.sql"):
        if int(source.name.split("_", 1)[0]) <= 47:
            shutil.copy2(source, old_migrations / source.name)
    pool = SqlitePool(tmp_path / "artifact-category.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            run_migrations(pool)
        with pool.transaction() as conn:
            assert "business_category" not in {
                row["name"] for row in conn.execute("PRAGMA table_info(project_task_artifacts)")
            }
            owner = conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) "
                "VALUES('category-owner','x','user',1) RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) "
                "VALUES('category-task',?,'Keep','category-request',123,456)",
                (owner,),
            )
            conn.execute(
                "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
                "configuration_json,model_ref,created_at,updated_at) "
                "VALUES('category-turn','category-task','category-request','Work','completed',"
                "'{}','',123,456)"
            )
            conn.execute(
                "INSERT INTO project_task_artifacts(artifact_id,task_id,turn_id,tool_call_id,"
                "request_hash,name,media_type,byte_count,sha256,source_path,content,created_at) "
                "VALUES('category-artifact','category-task','category-turn','call','legacy-hash',"
                "'report.pdf','application/pdf',3,'digest','output/report.pdf',?,123)",
                (b"\x00\xffx",),
            )
            before = dict(conn.execute("SELECT * FROM project_task_artifacts").fetchone())
            conn.execute("UPDATE _schema_version SET version=?", (recorded_version,))
        if apply_numbered_sql:
            with pool.connect() as conn:
                conn.executescript(
                    (
                        migrate._MIGRATIONS_DIR / "048_task_artifact_business_category.sql"
                    ).read_text()
                )
        run_migrations(pool)
        run_migrations(pool)
        with pool.connect() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
            after = dict(conn.execute("SELECT * FROM project_task_artifacts").fetchone())
            assert after.pop("business_category") == "document"
            assert after == before
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with pool.transaction() as conn:
            for category in ("document", "presentation", "spreadsheet", "image", "other"):
                conn.execute("UPDATE project_task_artifacts SET business_category=?", (category,))
            for category in ("report", "data", "workflow", "uncategorized", "all", "", None):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE project_task_artifacts SET business_category=?", (category,)
                    )
    finally:
        pool.close()


@pytest.mark.parametrize(
    "recorded_version,apply_numbered_sql", [(48, False), (48, True), (49, False), (99, False)]
)
def test_v48_suffix_category_backfill_preserves_snapshots_ids_and_sequence(
    tmp_path, monkeypatch, recorded_version, apply_numbered_sql
):
    import shutil

    from octop.infra.db import migrate
    from octop.infra.utils.artifact_kinds import ARTIFACT_KIND_RULES

    old_migrations = tmp_path / "v48-migrations"
    old_migrations.mkdir()
    for source in migrate._MIGRATIONS_DIR.glob("*.sql"):
        if int(source.name.split("_", 1)[0]) <= 48:
            shutil.copy2(source, old_migrations / source.name)
    examples = []
    for kind, extensions, _ in ARTIFACT_KIND_RULES:
        category = (
            "document"
            if kind in {"document", "text", "code"}
            else kind
            if kind in {"presentation", "spreadsheet", "image"}
            else "other"
        )
        examples.extend(("报告" + suffix.upper(), "image/png", category) for suffix in extensions)
    examples.extend(
        [
            ("mime.pdf.zip", "application/pdf", "other"),
            ("README", "application/pdf", "other"),
            ("unknown.file", "image/png", "other"),
            ("config.İNI", "text/plain", "other"),
        ]
    )
    pool = SqlitePool(tmp_path / "v48-categories.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            run_migrations(pool)
        with pool.transaction() as conn:
            owner = conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) "
                "VALUES('suffix-owner','x','user',1) RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) "
                "VALUES('suffix-task',?,'Keep','suffix-request',123,456)",
                (owner,),
            )
            conn.execute(
                "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
                "configuration_json,model_ref,created_at,updated_at) "
                "VALUES('suffix-turn','suffix-task','suffix-request','Work','completed',"
                "'{}','',123,456)"
            )
            old_categories = ("report", "data", "workflow", "uncategorized")
            for index, (name, media_type, _) in enumerate(examples):
                conn.execute(
                    "INSERT INTO project_task_artifacts(id,artifact_id,task_id,turn_id,tool_call_id,"
                    "request_hash,name,media_type,byte_count,sha256,source_path,content,created_at,"
                    "business_category) VALUES(?,?,'suffix-task','suffix-turn',?,'legacy-hash',"
                    "?,?,3,'digest',?, ?,123,?)",
                    (
                        index + 1,
                        f"suffix-{index}",
                        f"call-{index}",
                        name,
                        media_type,
                        f"output/{name}",
                        b"\x00\xffx",
                        old_categories[index % 4],
                    ),
                )
            conn.execute(
                "INSERT INTO project_task_artifacts(id,artifact_id,task_id,turn_id,tool_call_id,"
                "request_hash,name,media_type,byte_count,sha256,source_path,content,created_at) "
                "VALUES(10000,'deleted-last','suffix-task','suffix-turn','deleted-call','hash',"
                "'deleted.pdf','application/pdf',0,'digest',NULL,?,123)",
                (b"",),
            )
            conn.execute("DELETE FROM project_task_artifacts WHERE artifact_id='deleted-last'")
            before = [
                dict(row)
                for row in conn.execute("SELECT * FROM project_task_artifacts ORDER BY id")
            ]
            conn.execute("UPDATE _schema_version SET version=?", (recorded_version,))
        if apply_numbered_sql:
            with pool.connect() as conn:
                conn.executescript(
                    (
                        migrate._MIGRATIONS_DIR / "049_task_artifact_suffix_categories.sql"
                    ).read_text()
                )
        run_migrations(pool)
        run_migrations(pool)
        with pool.connect() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 57
            after = [
                dict(row)
                for row in conn.execute("SELECT * FROM project_task_artifacts ORDER BY id")
            ]
            assert len(after) == len(before) == len(examples)
            for old, new, (_, _, expected) in zip(before, after, examples, strict=True):
                old.pop("business_category")
                assert new.pop("business_category") == expected
                assert old == new
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert {"idx_task_artifacts_created", "idx_task_artifacts_task_created"} <= {
                row["name"] for row in conn.execute("PRAGMA index_list(project_task_artifacts)")
            }
            assert (
                conn.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name='project_task_artifacts'"
                ).fetchone()[0]
                == 10000
            )
        with pool.transaction() as conn:
            next_id = conn.execute(
                "INSERT INTO project_task_artifacts(artifact_id,task_id,turn_id,tool_call_id,"
                "request_hash,name,media_type,byte_count,sha256,source_path,content,created_at) "
                "VALUES('after-upgrade','suffix-task','suffix-turn','new-call','hash',"
                "'new.bin','application/pdf',0,'digest',NULL,?,123) RETURNING id,business_category",
                (b"",),
            ).fetchone()
            assert next_id["id"] == 10001
            assert next_id["business_category"] == "other"
    finally:
        pool.close()
