"""SQLite migration and ownership constraints for the admission foundation.

PostgreSQL checks below compare the shipped DDL, not a live PostgreSQL server.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from octop.config import OctopConfig
from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout

MIGRATIONS = Path(__file__).resolve().parents[3] / "src/octop/infra/db/migrations"
SQLITE_DDL = MIGRATIONS / "021_agentcomputer_core.sql"
POSTGRES_DDL = MIGRATIONS / "021_agentcomputer_core.pg.sql"
PUBLIC_IDS = {
    "ac_projects": "project_id",
    "ac_tasks": "task_id",
    "ac_turns": "turn_id",
    "ac_runs": "run_id",
    "ac_commands": "command_id",
    "ac_events": "event_id",
}


def _id() -> str:
    return str(uuid4())


def _insert(pool: SqlitePool, table: str, values: dict[str, Any]) -> None:
    # Table/column names come exclusively from the test's fixed schema fixtures.
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    with pool.connect() as conn:
        conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values())
        )


def _tables(pool: SqlitePool) -> set[str]:
    with pool.connect() as conn:
        return {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def _version(pool: SqlitePool) -> int:
    with pool.connect() as conn:
        return int(conn.execute("SELECT version FROM _schema_version").fetchone()[0])


def _legacy_pool(path: Path) -> SqlitePool:
    """Build the canonical v20 DDL, without involving the v21 repair path."""
    pool = SqlitePool(path)
    paths = sorted(
        p
        for p in MIGRATIONS.glob("*.sql")
        if not p.name.endswith(".pg.sql") and int(p.name[:3]) <= 20
    )
    with pool.connect() as conn:
        for migration_path in paths:
            conn.executescript(migration_path.read_text(encoding="utf-8"))
    assert _version(pool) == 20
    assert not set(PUBLIC_IDS) & _tables(pool)
    return pool


@pytest.fixture
def db(tmp_path: Path) -> Iterator[SqlitePool]:
    pool = SqlitePool(tmp_path / "control.db")
    try:
        migrate.run_migrations(pool)
        yield pool
    finally:
        pool.close()


def _graph(pool: SqlitePool, owner_id: int | None = None) -> dict[str, dict[str, Any]]:
    if owner_id is None:
        owner_id = UserRepo(pool).create(username=f"owner-{uuid4().hex}", role="user")
    project_id, task_id, turn_id, run_id = (_id() for _ in range(4))
    rows: dict[str, dict[str, Any]] = {
        "ac_projects": {
            "project_id": project_id,
            "owner_id": owner_id,
            "box_id": _id(),
            "workspace_id": _id(),
            "config_json": "{}",
            "config_digest": "sha256:fixture-project",
            "created_at": 1000,
            "updated_at": 1000,
        },
        "ac_tasks": {
            "task_id": task_id,
            "project_id": project_id,
            "owner_id": owner_id,
            "session_id": _id(),
            "title": "Fixture task",
            "created_at": 1000,
            "updated_at": 1000,
        },
        "ac_turns": {
            "turn_id": turn_id,
            "task_id": task_id,
            "project_id": project_id,
            "owner_id": owner_id,
            "user_message_id": _id(),
            "assistant_message_id": _id(),
            "context_mode": "newMessage",
            "snapshot_json": "{}",
            "snapshot_digest": "sha256:fixture-snapshot",
            "created_at": 1000,
        },
        "ac_runs": {
            "run_id": run_id,
            "turn_id": turn_id,
            "task_id": task_id,
            "project_id": project_id,
            "owner_id": owner_id,
            "state": "succeeded",
            "dispatch_phase": "notDispatched",
            "created_at": 1000,
            "updated_at": 1000,
        },
        "ac_commands": {
            "command_id": _id(),
            "owner_id": owner_id,
            "idempotency_key": _id(),
            "operation": "fixture.record",
            "target_id": task_id,
            "payload_digest": "sha256:fixture-command",
            "restore_epoch": 1,
            "receipt_json": "{}",
            "created_at": 1000,
        },
        "ac_events": {
            "event_id": _id(),
            "task_id": task_id,
            "owner_id": owner_id,
            "sequence": 1,
            "run_id": run_id,
            "turn_id": turn_id,
            "event_type": "run.finished",
            "payload_version": 1,
            "payload_json": '{"state":"succeeded"}',
            "created_at": 1000,
        },
    }
    for table, row in rows.items():
        _insert(pool, table, row)
    return rows


def _next_turn(pool: SqlitePool, template: dict[str, Any]) -> dict[str, Any]:
    row = {**template, "turn_id": _id(), "user_message_id": _id(), "assistant_message_id": _id()}
    _insert(pool, "ac_turns", row)
    return row


def _next_run(template: dict[str, Any], turn: dict[str, Any]) -> dict[str, Any]:
    return {**template, "run_id": _id(), "turn_id": turn["turn_id"]}


def test_empty_database_gets_complete_core_schema(db: SqlitePool) -> None:
    assert _version(db) == 25
    assert set(PUBLIC_IDS) <= _tables(db)
    with db.connect() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("watermark", [20, 21, 999])
def test_v20_and_skipped_watermarks_preserve_users_and_create_core(
    tmp_path: Path, watermark: int
) -> None:
    pool = _legacy_pool(tmp_path / "legacy.db")
    try:
        users = UserRepo(pool)
        owner_id = users.create(username="kept-user", password_hash="kept-hash", role="admin")
        with pool.connect() as conn:
            before = dict(conn.execute("SELECT * FROM users WHERE id = ?", (owner_id,)).fetchone())
            conn.execute("UPDATE _schema_version SET version = ?", (watermark,))
        migrate.run_migrations(pool)
        assert _version(pool) == 25
        assert set(PUBLIC_IDS) <= _tables(pool)
        with pool.connect() as conn:
            after = dict(conn.execute("SELECT * FROM users WHERE id = ?", (owner_id,)).fetchone())
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert after == before
    finally:
        pool.close()


def test_repeated_migration_preserves_populated_core(db: SqlitePool) -> None:
    _graph(db)
    with db.connect() as conn:
        before = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in PUBLIC_IDS
        }
    migrate.run_migrations(db)
    migrate.run_migrations(db)
    with db.connect() as conn:
        after = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in PUBLIC_IDS
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert after == before


def test_sqlite_failed_ddl_rolls_back_schema_and_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _legacy_pool(tmp_path / "legacy.db")
    broken = tmp_path / "021_failed.sql"
    broken.write_text(
        SQLITE_DDL.read_text(encoding="utf-8").replace(
            "UPDATE _schema_version SET version = 21;",
            "INSERT INTO intentionally_missing_ac_table VALUES (1);\n"
            "UPDATE _schema_version SET version = 21;",
        ),
        encoding="utf-8",
    )
    try:
        owner_id = UserRepo(pool).create(username="survivor", role="user")
        original_discover = migrate._discover

        def faulting_discovery(dialect: str) -> list[tuple[int, Path]]:
            return [
                (version, broken if version == 21 else path)
                for version, path in original_discover(dialect)
            ]

        with monkeypatch.context() as scoped_patch:
            scoped_patch.setattr(migrate, "_discover", faulting_discovery)
            with pytest.raises(sqlite3.OperationalError, match="intentionally_missing_ac_table"):
                migrate.run_migrations(pool)
        assert _version(pool) == 20
        assert not set(PUBLIC_IDS) & _tables(pool)
        assert UserRepo(pool).get(owner_id) is not None
        with pool.connect() as conn:
            assert not conn.in_transaction
        migrate.run_migrations(pool)
        assert _version(pool) == 25
    finally:
        pool.close()


@pytest.mark.parametrize("table,public_id", PUBLIC_IDS.items())
def test_resource_identity_is_integer_pk_and_unique_public_uuid(
    db: SqlitePool, table: str, public_id: str
) -> None:
    rows = _graph(db)
    original = rows[table]
    with db.connect() as conn:
        stored = conn.execute(
            f"SELECT * FROM {table} WHERE {public_id} = ?", (original[public_id],)
        ).fetchone()
        assert isinstance(stored["id"], int) and stored["id"] > 0
        assert str(UUID(stored[public_id])) == original[public_id]
    duplicate = dict(original)
    if table == "ac_projects":
        duplicate["workspace_id"] = _id()
    elif table == "ac_tasks":
        duplicate["session_id"] = _id()
    elif table == "ac_turns":
        duplicate.update(user_message_id=_id(), assistant_message_id=_id())
    elif table == "ac_runs":
        duplicate["turn_id"] = _next_turn(db, rows["ac_turns"])["turn_id"]
    elif table == "ac_commands":
        duplicate["idempotency_key"] = _id()
    elif table == "ac_events":
        duplicate["sequence"] = 2
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, table, duplicate)


@pytest.mark.parametrize("table", ["ac_tasks", "ac_turns", "ac_runs", "ac_events"])
def test_composite_parent_owner_cannot_be_reassigned(db: SqlitePool, table: str) -> None:
    first = _graph(db)
    second = _graph(db)
    row = {**first[table], PUBLIC_IDS[table]: _id(), "owner_id": second[table]["owner_id"]}
    if table == "ac_tasks":
        row["session_id"] = _id()
    elif table == "ac_turns":
        row.update(user_message_id=_id(), assistant_message_id=_id())
    elif table == "ac_runs":
        row["turn_id"] = _next_turn(db, first["ac_turns"])["turn_id"]
    else:
        row["sequence"] = 2
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert(db, table, row)


@pytest.mark.parametrize("table", ["ac_projects", "ac_commands"])
def test_resource_owner_must_exist(db: SqlitePool, table: str) -> None:
    original = _graph(db)[table]
    row = {**original, PUBLIC_IDS[table]: _id(), "owner_id": 999999}
    if table == "ac_projects":
        row["workspace_id"] = _id()
    else:
        row["idempotency_key"] = _id()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert(db, table, row)


def test_turn_and_run_must_match_the_full_parent_identity(db: SqlitePool) -> None:
    first = _graph(db)
    second = _graph(db, first["ac_projects"]["owner_id"])
    turn = {
        **first["ac_turns"],
        "turn_id": _id(),
        "project_id": second["ac_projects"]["project_id"],
        "user_message_id": _id(),
        "assistant_message_id": _id(),
    }
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert(db, "ac_turns", turn)
    run = {
        **first["ac_runs"],
        "run_id": _id(),
        "turn_id": _next_turn(db, first["ac_turns"])["turn_id"],
        "task_id": second["ac_tasks"]["task_id"],
        "project_id": second["ac_projects"]["project_id"],
    }
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert(db, "ac_runs", run)


def test_user_message_identity_is_unique_within_its_task(db: SqlitePool) -> None:
    first = _graph(db)
    second = _graph(db, first["ac_projects"]["owner_id"])
    shared_message = first["ac_turns"]["user_message_id"]
    other_task = {
        **second["ac_turns"],
        "turn_id": _id(),
        "user_message_id": shared_message,
        "assistant_message_id": _id(),
    }
    _insert(db, "ac_turns", other_task)
    same_task = {
        **first["ac_turns"],
        "turn_id": _id(),
        "assistant_message_id": _id(),
    }
    with pytest.raises(sqlite3.IntegrityError, match="ac_turns.task_id, ac_turns.user_message_id"):
        _insert(db, "ac_turns", same_task)


@pytest.mark.parametrize("restore_epoch", [0, -1])
def test_run_restore_epoch_must_be_positive(db: SqlitePool, restore_epoch: int) -> None:
    rows = _graph(db)
    run = {
        **_next_run(rows["ac_runs"], _next_turn(db, rows["ac_turns"])),
        "restore_epoch": restore_epoch,
    }
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert(db, "ac_runs", run)


@pytest.mark.parametrize(
    "state,reconcile",
    [
        ("queued", 0),
        ("running", 0),
        ("waitingApproval", 0),
        ("waitingInput", 0),
        ("interrupted", 1),
        ("failed", 1),
    ],
)
def test_active_or_unreconciled_run_blocks_another_run_in_same_task(
    db: SqlitePool, state: str, reconcile: int
) -> None:
    rows = _graph(db)
    with db.connect() as conn:
        conn.execute(
            "UPDATE ac_runs SET state = ?, reconciliation_required = ? WHERE run_id = ?",
            (state, reconcile, rows["ac_runs"]["run_id"]),
        )
    next_run = {**_next_run(rows["ac_runs"], _next_turn(db, rows["ac_turns"])), "state": "queued"}
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _insert(db, "ac_runs", next_run)


@pytest.mark.parametrize("state", ["succeeded", "failed", "stopped", "interrupted"])
def test_terminal_reconciled_run_allows_new_run(db: SqlitePool, state: str) -> None:
    rows = _graph(db)
    with db.connect() as conn:
        conn.execute(
            "UPDATE ac_runs SET state = ? WHERE run_id = ?", (state, rows["ac_runs"]["run_id"])
        )
    next_run = {**_next_run(rows["ac_runs"], _next_turn(db, rows["ac_turns"])), "state": "queued"}
    _insert(db, "ac_runs", next_run)


def test_different_tasks_cannot_hold_same_project_writer(db: SqlitePool) -> None:
    rows = _graph(db)
    with db.connect() as conn:
        conn.execute(
            "UPDATE ac_runs SET state = 'queued' WHERE run_id = ?", (rows["ac_runs"]["run_id"],)
        )
    task = {**rows["ac_tasks"], "task_id": _id(), "session_id": _id()}
    _insert(db, "ac_tasks", task)
    turn = _next_turn(db, {**rows["ac_turns"], "task_id": task["task_id"]})
    next_run = {**_next_run(rows["ac_runs"], turn), "task_id": task["task_id"], "state": "queued"}
    with pytest.raises(sqlite3.IntegrityError, match="ac_runs.project_id"):
        _insert(db, "ac_runs", next_run)


def test_wrong_task_active_run_is_rejected_at_commit(db: SqlitePool) -> None:
    rows = _graph(db)
    task = {**rows["ac_tasks"], "task_id": _id(), "session_id": _id()}
    _insert(db, "ac_tasks", task)
    turn = _next_turn(db, {**rows["ac_turns"], "task_id": task["task_id"]})
    run = {**_next_run(rows["ac_runs"], turn), "task_id": task["task_id"]}
    _insert(db, "ac_runs", run)
    updated = False
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), db.transaction() as conn:
        conn.execute(
            "UPDATE ac_tasks SET active_run_id = ? WHERE task_id = ?",
            (run["run_id"], rows["ac_tasks"]["task_id"]),
        )
        updated = True
    assert updated, "The FK must be deferred until commit, not rejected at UPDATE"
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT active_run_id FROM ac_tasks WHERE task_id = ?",
                (rows["ac_tasks"]["task_id"],),
            ).fetchone()[0]
            is None
        )


def test_wrong_project_active_run_is_rejected_at_commit(db: SqlitePool) -> None:
    first = _graph(db)
    second = _graph(db, first["ac_projects"]["owner_id"])
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), db.transaction() as conn:
        conn.execute(
            "UPDATE ac_projects SET active_run_id = ? WHERE project_id = ?",
            (second["ac_runs"]["run_id"], first["ac_projects"]["project_id"]),
        )


def test_valid_deferred_run_link_can_be_written_before_run(db: SqlitePool) -> None:
    rows = _graph(db)
    turn = _next_turn(db, rows["ac_turns"])
    run = {**_next_run(rows["ac_runs"], turn), "state": "queued"}
    with db.transaction() as conn:
        conn.execute(
            "UPDATE ac_tasks SET active_run_id = ? WHERE task_id = ?",
            (run["run_id"], run["task_id"]),
        )
        conn.execute(
            "UPDATE ac_projects SET active_run_id = ? WHERE project_id = ?",
            (run["run_id"], run["project_id"]),
        )
        _insert(db, "ac_runs", run)
    with db.connect() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_event_sequence_is_task_scoped_and_run_turn_pair_is_fixed(db: SqlitePool) -> None:
    first = _graph(db)
    second = _graph(db, first["ac_projects"]["owner_id"])
    duplicate = {**first["ac_events"], "event_id": _id()}
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _insert(db, "ac_events", duplicate)
    mismatched = {
        **first["ac_events"],
        "event_id": _id(),
        "sequence": 2,
        "run_id": second["ac_runs"]["run_id"],
        "turn_id": second["ac_turns"]["turn_id"],
    }
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert(db, "ac_events", mismatched)
    turn = _next_turn(db, first["ac_turns"])
    wrong_turn = {
        **first["ac_events"],
        "event_id": _id(),
        "sequence": 2,
        "turn_id": turn["turn_id"],
    }
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert(db, "ac_events", wrong_turn)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_events WHERE sequence = 1").fetchone()[0] == 2


def test_sqlite_and_postgresql_declared_core_constraints_match() -> None:
    """Static DDL parity only; this does not exercise PostgreSQL execution."""
    sqlite = SQLITE_DDL.read_text(encoding="utf-8")
    postgres = POSTGRES_DDL.read_text(encoding="utf-8")
    pattern = r"CREATE TABLE IF NOT EXISTS (ac_\w+) \((.*?)\n\);"
    sqlite_tables = dict(re.findall(pattern, sqlite, re.DOTALL))
    postgres_tables = dict(re.findall(pattern, postgres, re.DOTALL))
    assert set(sqlite_tables) == set(postgres_tables) == set(PUBLIC_IDS)
    for table, public_id in PUBLIC_IDS.items():
        sqlite_columns = re.findall(
            r"^  (\w+) (INTEGER|TEXT)\b", sqlite_tables[table], re.MULTILINE
        )
        pg_columns = re.findall(
            r"^  (\w+) (INTEGER|BIGINT|TEXT)\b", postgres_tables[table], re.MULTILINE
        )
        assert sqlite_columns == [
            (name, "INTEGER" if kind == "BIGINT" else kind) for name, kind in pg_columns
        ]
        assert "id INTEGER PRIMARY KEY AUTOINCREMENT" in sqlite_tables[table]
        assert "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" in postgres_tables[table]
        assert f"{public_id} TEXT NOT NULL UNIQUE" in sqlite_tables[table]
        assert f"{public_id} TEXT NOT NULL UNIQUE" in postgres_tables[table]
        assert re.findall(r"UNIQUE\s*\(([^)]*)\)", sqlite_tables[table]) == re.findall(
            r"UNIQUE\s*\(([^)]*)\)", postgres_tables[table]
        )
        inline_reference = r"^  (\w+) [^\n]*\bREFERENCES (\w+)\(([^)]*)\)"
        assert re.findall(inline_reference, sqlite_tables[table], re.MULTILINE) == re.findall(
            inline_reference, postgres_tables[table], re.MULTILINE
        )
        check_pattern = r"CHECK\s*\((.*?)\)(?=,?\n)"
        sqlite_checks = re.findall(check_pattern, sqlite_tables[table] + "\n", re.DOTALL)
        pg_checks = re.findall(check_pattern, postgres_tables[table] + "\n", re.DOTALL)
        assert [re.sub(r"\s+", " ", item).strip() for item in sqlite_checks] == [
            re.sub(r"\s+", " ", item).strip() for item in pg_checks
        ]
    fk_pattern = r"FOREIGN KEY\s*\(([^)]*)\)\s+REFERENCES\s+(\w+)\s*\(([^)]*)\)(\s+DEFERRABLE INITIALLY DEFERRED)?"

    def normalize(text: str) -> set[str]:
        return {
            re.sub(r"\s+", " ", " ".join(parts)).strip() for parts in re.findall(fk_pattern, text)
        }

    assert normalize(sqlite) == normalize(postgres)
    index_pattern = (
        r"CREATE UNIQUE INDEX IF NOT EXISTS idx_ac_runs_(\w+)\s+ON ac_runs\((\w+)\)\s+WHERE (.*?);"
    )
    assert [
        tuple(re.sub(r"\s+", " ", part).strip() for part in row)
        for row in re.findall(index_pattern, sqlite, re.DOTALL)
    ] == [
        tuple(re.sub(r"\s+", " ", part).strip() for part in row)
        for row in re.findall(index_pattern, postgres, re.DOTALL)
    ]


def test_core_repositories_are_wired_through_shared_services(
    db: SqlitePool, tmp_path: Path
) -> None:
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    for resource in ("project", "task", "turn", "run", "command", "event"):
        name = f"ac_{resource}_repo"
        assert getattr(services, name) is getattr(services.repos, name)
