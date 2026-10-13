"""Project preparation persistence without model or workspace execution.

PostgreSQL assertions compare paired DDL only; no PostgreSQL server is used.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from octop.config import OctopConfig
from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.ac_project_preparations import AcProjectPreparationRepo
from octop.infra.db.repos.ac_projects import AcProjectRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout

MIGRATIONS = Path(__file__).resolve().parents[3] / "src/octop/infra/db/migrations"
SQLITE_DDL = MIGRATIONS / "022_agentcomputer_projects.sql"
POSTGRES_DDL = MIGRATIONS / "022_agentcomputer_projects.pg.sql"


def _id() -> str:
    return str(uuid4())


def _version(pool: SqlitePool) -> int:
    with pool.connect() as conn:
        return int(conn.execute("SELECT version FROM _schema_version").fetchone()[0])


def _columns(pool: SqlitePool, table: str) -> set[str]:
    with pool.connect() as conn:
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _v21_pool(path: Path) -> SqlitePool:
    """Canonical applied v21 is a real upgrade input, not a changed v22 fixture."""
    pool = SqlitePool(path)
    paths = sorted(
        p
        for p in MIGRATIONS.glob("*.sql")
        if not p.name.endswith(".pg.sql") and int(p.name[:3]) <= 21
    )
    with pool.connect() as conn:
        for migration_path in paths:
            conn.executescript(migration_path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO users(id,username,password_hash,role,created_at) "
            "VALUES(1,'owner-one','fixture','admin',1)"
        )
        conn.execute(
            "INSERT INTO users(id,username,password_hash,role,created_at) "
            "VALUES(2,'owner-two','fixture','user',1)"
        )
    assert _version(pool) == 21
    assert "name" not in _columns(pool, "ac_projects")
    assert "managed_by" not in _columns(pool, "agents")
    return pool


def _minimal_legacy_pool(path: Path, watermark: int) -> SqlitePool:
    pool = SqlitePool(path)
    with pool.connect() as conn:
        conn.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL, "
            "display_name TEXT, disabled INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);\n"
            "CREATE TABLE _schema_version(version INTEGER NOT NULL);\n"
            "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);\n"
            "CREATE TABLE legacy_notes(note TEXT NOT NULL);\n"
            "INSERT INTO users(username,password_hash,role,display_name,created_at) "
            "VALUES('legacy','kept-hash','admin','原用户',17);\n"
            "INSERT INTO settings(key,value) VALUES('active_stt_provider','kept-provider');\n"
            "INSERT INTO legacy_notes(note) VALUES('kept-note');\n"
        )
        conn.execute("INSERT INTO _schema_version(version) VALUES(?)", (watermark,))
    return pool


@pytest.fixture
def db(tmp_path: Path) -> Iterator[SqlitePool]:
    pool = _v21_pool(tmp_path / "control.db")
    try:
        migrate.run_migrations(pool)
        yield pool
    finally:
        pool.close()


def _project(pool: SqlitePool, *, owner_id: int = 1) -> dict[str, Any]:
    fields = {
        "project_id": _id(),
        "owner_id": owner_id,
        "box_id": _id(),
        "workspace_id": _id(),
        "name": "准备项目",
        "config_json": '{"instructions":"你好","modelRef":"fixture/no-model","schemaVersion":1}',
        "config_digest": "sha256:fixture-configuration",
        "created_at": 1000,
        "updated_at": 1000,
    }
    with pool.transaction() as conn:
        row = AcProjectRepo.insert(conn, **fields)
        assert row is not None
        assert row.name == fields["name"]
    return fields


def _preparation(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "preparation_id": _id(),
        "project_id": project["project_id"],
        "owner_id": project["owner_id"],
        "planned_agent_id": "ac-" + uuid4().hex[:12],
        "intent_json": '{"schemaVersion":1,"serverRoot":"private-fixture-root"}',
        "intent_digest": "sha256:fixture-intent",
        "restore_epoch": 3,
        "created_at": 1000,
        "updated_at": 1000,
    }


def _insert_preparation(pool: SqlitePool, fields: dict[str, Any]) -> None:
    with pool.transaction() as conn:
        AcProjectPreparationRepo.insert(conn, **fields)


def _agent(pool: SqlitePool, project_id: str, *, owner_id: int = 1) -> str:
    agent_id = "ac-" + uuid4().hex[:12]
    with pool.transaction() as conn:
        conn.execute(
            "INSERT INTO agents(agent_id,user_id,name,managed_by,managed_project_id,"
            "created_at,updated_at) VALUES(?,?,?,'agentcomputer',?,1,1)",
            (agent_id, owner_id, agent_id, project_id),
        )
    return agent_id


def test_v21_upgrade_preserves_project_agent_and_original_receipt(tmp_path: Path) -> None:
    pool = _v21_pool(tmp_path / "old.db")
    project_id, agent_id, command_id = _id(), "existing-agent", _id()
    receipt = '{ "protocolVersion": 1, "commandID": "' + command_id + '", "accepted": true }'
    try:
        with pool.connect() as conn:
            conn.execute(
                "INSERT INTO agents(agent_id,user_id,name,created_at,updated_at) VALUES(?,1,?,1,1)",
                (agent_id, "原专家"),
            )
            conn.execute(
                "INSERT INTO ac_projects(project_id,owner_id,box_id,workspace_id,"
                "default_agent_id,config_json,config_digest,created_at,updated_at) "
                "VALUES(?,1,?,?,?,'{}','sha256:original',1,1)",
                (project_id, _id(), _id(), agent_id),
            )
            conn.execute(
                "INSERT INTO ac_commands(command_id,owner_id,idempotency_key,operation,target_id,"
                "payload_digest,restore_epoch,receipt_json,created_at) "
                "VALUES(?,1,?,'turn.newMessage',?,'sha256:original',1,?,1)",
                (command_id, _id(), _id(), receipt),
            )
            before_project = dict(conn.execute("SELECT * FROM ac_projects").fetchone())
            before_agent = dict(conn.execute("SELECT * FROM agents").fetchone())
            before_command = dict(conn.execute("SELECT * FROM ac_commands").fetchone())
        migrate.run_migrations(pool)
        migrate.run_migrations(pool)
        assert _version(pool) == 25
        with pool.connect() as conn:
            after_project = dict(conn.execute("SELECT * FROM ac_projects").fetchone())
            after_agent = dict(conn.execute("SELECT * FROM agents").fetchone())
            assert after_project.pop("name") == ""
            assert after_agent.pop("managed_by") is None
            assert after_agent.pop("managed_project_id") is None
            assert after_project == before_project
            assert {key: after_agent[key] for key in before_agent} == before_agent
            # Raw v21 DDL can still need the older profile-column repair helpers.
            assert set(after_agent) - set(before_agent) <= {"knowledge_base_ids", "mcp_servers"}
            assert dict(conn.execute("SELECT * FROM ac_commands").fetchone()) == before_command
            assert conn.execute("SELECT receipt_json FROM ac_commands").fetchone()[0] == receipt
            assert conn.execute("SELECT COUNT(*) FROM ac_project_preparations").fetchone()[0] == 0
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


@pytest.mark.parametrize("watermark", [8, 21, 22, 999])
def test_minimal_legacy_users_database_materializes_foundation_and_v22(
    tmp_path: Path, watermark: int
) -> None:
    pool = _minimal_legacy_pool(tmp_path / "minimal.db", watermark)
    try:
        with pool.connect() as conn:
            before = dict(conn.execute("SELECT * FROM users").fetchone())
        migrate.run_migrations(pool)
        migrate.run_migrations(pool)
        assert _version(pool) == 25
        assert {"managed_by", "managed_project_id", "kind", "is_shared"} <= _columns(pool, "agents")
        assert "name" in _columns(pool, "ac_projects")
        assert "preparation_id" in _columns(pool, "ac_project_preparations")
        canonical = (MIGRATIONS / "001_initial.sql").read_text(encoding="utf-8")
        required_tables = set(re.findall(r"CREATE TABLE (\w+)\b", canonical))
        with pool.connect() as conn:
            existing = {
                str(row["name"])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert required_tables <= existing
            after = dict(conn.execute("SELECT * FROM users").fetchone())
            assert {key: after[key] for key in before} == before
            assert after["locale"] == "zh"
            assert conn.execute("SELECT value FROM settings").fetchall()[0][0] == "kept-provider"
            assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 1
            assert conn.execute("SELECT note FROM legacy_notes").fetchone()[0] == "kept-note"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        _insert_preparation(pool, _preparation(_project(pool)))
    finally:
        pool.close()


def test_missing_initial_tables_repair_failure_preserves_legacy_watermark_and_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _minimal_legacy_pool(tmp_path / "foundation-rollback.db", 8)
    source = MIGRATIONS / "001_initial.sql"
    broken = tmp_path / "001_broken.sql"
    broken.write_text(
        source.read_text(encoding="utf-8")
        + "\nCREATE TABLE broken_initial AS SELECT * FROM missing_initial_source;\n",
        encoding="utf-8",
    )
    original = migrate._discover

    def discovery(dialect: str) -> list[tuple[int, Path]]:
        return [(version, broken if version == 1 else path) for version, path in original(dialect)]

    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_discover", discovery)
            with pytest.raises(sqlite3.OperationalError, match="missing_initial_source"):
                migrate.run_migrations(pool)
        assert _version(pool) == 8
        assert _columns(pool, "agents") == set()
        assert "locale" not in _columns(pool, "users")
        with pool.connect() as conn:
            assert (
                conn.execute("SELECT username,password_hash FROM users").fetchone()[0] == "legacy"
            )
            assert conn.execute("SELECT value FROM settings").fetchone()[0] == "kept-provider"
            assert conn.execute("SELECT note FROM legacy_notes").fetchone()[0] == "kept-note"
            assert not conn.in_transaction
        migrate.run_migrations(pool)
        assert _version(pool) == 25
    finally:
        pool.close()


@pytest.mark.parametrize("watermark", [21, 22, 999])
@pytest.mark.parametrize("present_column", [None, "name", "managed_by", "managed_project_id"])
def test_skipped_watermarks_and_partial_columns_repair_atomically(
    tmp_path: Path, watermark: int, present_column: str | None
) -> None:
    pool = _v21_pool(tmp_path / "partial.db")
    try:
        with pool.connect() as conn:
            if present_column == "name":
                conn.execute("ALTER TABLE ac_projects ADD COLUMN name TEXT NOT NULL DEFAULT ''")
            elif present_column == "managed_by":
                conn.execute(
                    "ALTER TABLE agents ADD COLUMN managed_by TEXT "
                    "CHECK (managed_by IS NULL OR managed_by = 'agentcomputer')"
                )
            elif present_column == "managed_project_id":
                conn.execute(
                    "ALTER TABLE agents ADD COLUMN managed_project_id TEXT "
                    "REFERENCES ac_projects(project_id)"
                )
            conn.execute("UPDATE _schema_version SET version = ?", (watermark,))
        migrate.run_migrations(pool)
        migrate.run_migrations(pool)
        assert _version(pool) == 25
        assert "name" in _columns(pool, "ac_projects")
        assert {"managed_by", "managed_project_id"} <= _columns(pool, "agents")
        assert "preparation_id" in _columns(pool, "ac_project_preparations")
        with pool.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO agents(agent_id,user_id,name,managed_by,created_at,updated_at) "
                "VALUES('invalid-managed',1,'invalid','agentcomputer',1,1)"
            )
    finally:
        pool.close()


def test_sqlite_v22_ddl_failure_rolls_back_columns_journal_and_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _v21_pool(tmp_path / "rollback.db")
    broken = tmp_path / "022_broken.sql"
    broken.write_text(
        SQLITE_DDL.read_text(encoding="utf-8").replace(
            "UPDATE _schema_version SET version = 22;",
            "INSERT INTO missing_preparation_table VALUES (1);\n"
            "UPDATE _schema_version SET version = 22;",
        ),
        encoding="utf-8",
    )
    original = migrate._discover

    def discovery(dialect: str) -> list[tuple[int, Path]]:
        return [(version, broken if version == 22 else path) for version, path in original(dialect)]

    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_discover", discovery)
            with pytest.raises(sqlite3.OperationalError, match="missing_preparation_table"):
                migrate.run_migrations(pool)
        assert _version(pool) == 21
        assert "name" not in _columns(pool, "ac_projects")
        assert not {"managed_by", "managed_project_id"} & _columns(pool, "agents")
        assert _columns(pool, "ac_project_preparations") == set()
        with pool.connect() as conn:
            assert not conn.in_transaction
        migrate.run_migrations(pool)
        assert _version(pool) == 25
    finally:
        pool.close()


def test_v22_missing_journal_is_repaired_without_rewriting_project_or_agent(db: SqlitePool) -> None:
    project = _project(db)
    agent = _agent(db, project["project_id"])
    with db.connect() as conn:
        before_project = dict(conn.execute("SELECT * FROM ac_projects").fetchone())
        before_agent = dict(
            conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent,)).fetchone()
        )
        conn.execute("DROP TABLE ac_project_preparations")
    assert _version(db) == 25
    migrate.run_migrations(db)
    assert _version(db) == 25
    with db.connect() as conn:
        assert dict(conn.execute("SELECT * FROM ac_projects").fetchone()) == before_project
        assert (
            dict(conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent,)).fetchone())
            == before_agent
        )
    _insert_preparation(db, _preparation(project))


@pytest.mark.parametrize("upgrade", ["helper", "canonical"])
@pytest.mark.parametrize(
    "managed_by,project_kind,error",
    [
        (None, "existing", "CHECK"),
        ("agentcomputer", None, "CHECK"),
        ("other", "existing", "CHECK"),
        ("agentcomputer", "missing", "FOREIGN KEY"),
    ],
)
def test_agent_managed_pair_and_project_fk_cannot_be_bypassed(
    tmp_path: Path, upgrade: str, managed_by: str | None, project_kind: str | None, error: str
) -> None:
    pool = _v21_pool(tmp_path / "constraints.db")
    try:
        if upgrade == "canonical":
            with pool.connect() as conn:
                conn.executescript(SQLITE_DDL.read_text(encoding="utf-8"))
        else:
            migrate.run_migrations(pool)
        project = _project(pool)
        project_id = project["project_id"] if project_kind == "existing" else None
        if project_kind == "missing":
            project_id = _id()
        with pool.connect() as conn, pytest.raises(sqlite3.IntegrityError, match=error):
            conn.execute(
                "INSERT INTO agents(agent_id,user_id,name,managed_by,managed_project_id,"
                "created_at,updated_at) VALUES('invalid',1,'invalid',?,?,1,1)",
                (managed_by, project_id),
            )
    finally:
        pool.close()


def test_preparation_identity_owner_fk_and_unique_reserved_agent(db: SqlitePool) -> None:
    project = _project(db)
    fields = _preparation(project)
    _insert_preparation(db, fields)
    row = AcProjectPreparationRepo(db).get(project["project_id"], 1)
    assert row is not None
    assert isinstance(row.pk, int) and row.pk > 0 and row.id == fields["preparation_id"]
    assert row.agent_state == row.workspace_state == "pending"
    assert row.restore_epoch == 3 and row.revision == row.schema_version == 1
    assert AcProjectPreparationRepo(db).get(project["project_id"], 2) is None
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _insert_preparation(
            db, {**fields, "preparation_id": _id(), "planned_agent_id": "different"}
        )
    second = _project(db)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _insert_preparation(
            db, {**fields, "preparation_id": _id(), "project_id": second["project_id"]}
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _insert_preparation(
            db, {**fields, "project_id": second["project_id"], "planned_agent_id": "different"}
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert_preparation(db, {**_preparation(second), "owner_id": 2})
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert_preparation(db, {**_preparation(second), "project_id": _id()})


@pytest.mark.parametrize(
    "column,value",
    [
        ("restore_epoch", 0),
        ("schema_version", 0),
        ("revision", 0),
        ("retryable", 2),
        ("agent_state", "ready"),
        ("workspace_state", "ready"),
        ("created_at", -1),
        ("updated_at", -1),
    ],
)
def test_preparation_schema_rejects_invalid_values(db: SqlitePool, column: str, value: Any) -> None:
    fields = _preparation(_project(db))
    fields[column] = value
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    with db.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            f"INSERT INTO ac_project_preparations ({columns}) VALUES ({placeholders})",
            tuple(fields.values()),
        )


def test_project_insert_conflicts_do_not_abort_transaction_or_reveal_existing_row(
    db: SqlitePool,
) -> None:
    project = _project(db)
    with db.transaction() as conn:
        assert AcProjectRepo.insert(conn, **{**project, "owner_id": 2}) is None
        assert AcProjectRepo.insert(conn, **{**project, "project_id": _id()}) is None
        another = {**project, "project_id": _id(), "workspace_id": _id()}
        assert AcProjectRepo.insert(conn, **another) is not None
    assert AcProjectRepo(db).get(project["project_id"], 2) is None


def test_progress_is_idempotent_keeps_intent_and_does_not_change_project_config_revision(
    db: SqlitePool,
) -> None:
    project = _project(db)
    fields = _preparation(project)
    _insert_preparation(db, fields)
    prep_repo = AcProjectPreparationRepo(db)
    project_repo = AcProjectRepo(db)
    agent = _agent(db, project["project_id"])
    with db.transaction() as conn:
        project_repo.set_preparation(conn, project["project_id"], 1, "preparing", updated_at=2000)
        assert project_repo.bind_agent(conn, project["project_id"], 1, agent, updated_at=2000)
        prep_repo.confirm_agent(conn, project["project_id"], 1, updated_at=2000)
        prep_repo.confirm_agent(conn, project["project_id"], 1, updated_at=3000)
        prep_repo.confirm_workspace(conn, project["project_id"], 1, updated_at=3000)
        prep_repo.confirm_workspace(conn, project["project_id"], 1, updated_at=4000)
        prep_repo.fail(
            conn, project["project_id"], 1, "managedIngressUnavailable", False, updated_at=4000
        )
        project_repo.set_preparation(conn, project["project_id"], 1, "failed", updated_at=4000)
    failed = prep_repo.get(project["project_id"], 1)
    assert failed is not None and failed.revision == 4
    assert failed.reason_code == "managedIngressUnavailable" and not failed.retryable
    with db.transaction() as conn:
        prep_repo.clear_reason(conn, project["project_id"], 1, updated_at=5000)
        prep_repo.clear_reason(conn, project["project_id"], 1, updated_at=6000)
    final = prep_repo.get_by_project(project["project_id"], 1)
    assert final is not None and final.revision == 5
    assert final.agent_state == final.workspace_state == "verified"
    assert final.reason_code is None and not final.retryable and final.updated_at == 5000
    assert (
        final.intent_json == fields["intent_json"]
        and final.intent_digest == fields["intent_digest"]
    )
    assert final.restore_epoch == 3 and final.planned_agent_id == fields["planned_agent_id"]
    stored = project_repo.get(project["project_id"], 1)
    assert stored is not None and stored.revision == 1 and stored.preparation_state == "failed"
    assert (
        stored.config_json == project["config_json"]
        and stored.config_digest == project["config_digest"]
    )


def test_bind_and_progress_wrong_owner_or_rebinding_do_not_modify_facts(db: SqlitePool) -> None:
    project = _project(db)
    fields = _preparation(project)
    _insert_preparation(db, fields)
    first = _agent(db, project["project_id"])
    second = _agent(db, project["project_id"])
    repo = AcProjectRepo(db)
    prep = AcProjectPreparationRepo(db)
    with db.transaction() as conn:
        assert repo.bind_agent(conn, project["project_id"], 1, first, updated_at=2000)
        assert repo.bind_agent(conn, project["project_id"], 1, first, updated_at=2000)
        assert not repo.bind_agent(conn, project["project_id"], 1, second, updated_at=3000)
        assert not repo.bind_agent(conn, project["project_id"], 2, second, updated_at=3000)
        repo.set_preparation(conn, project["project_id"], 2, "failed", updated_at=3000)
        prep.confirm_agent(conn, project["project_id"], 2, updated_at=3000)
        prep.confirm_workspace(conn, project["project_id"], 2, updated_at=3000)
        prep.fail(conn, project["project_id"], 2, "wrongOwner", True, updated_at=3000)
        prep.clear_reason(conn, project["project_id"], 2, updated_at=4000)
    row = repo.get(project["project_id"], 1)
    assert row is not None and row.default_agent_id == first and row.preparation_state == "pending"
    preparation = prep.get(project["project_id"], 1)
    assert preparation is not None and preparation.revision == 1 and preparation.reason_code is None


def test_project_preparation_and_binding_roll_back_with_caller_transaction(db: SqlitePool) -> None:
    project = _project(db)
    fields = _preparation(project)
    agent = _agent(db, project["project_id"])
    with pytest.raises(RuntimeError, match="injected"), db.transaction() as conn:
        AcProjectPreparationRepo.insert(conn, **fields)
        AcProjectRepo.bind_agent(conn, project["project_id"], 1, agent, updated_at=2000)
        AcProjectPreparationRepo.confirm_agent(conn, project["project_id"], 1, updated_at=2000)
        raise RuntimeError("injected")
    assert AcProjectPreparationRepo(db).get(project["project_id"], 1) is None
    row = AcProjectRepo(db).get(project["project_id"], 1)
    assert row is not None and row.default_agent_id is None and row.revision == 1


def test_preparation_reopens_with_stable_identity_and_shared_services(tmp_path: Path) -> None:
    database = tmp_path / "reopen.db"
    pool = _v21_pool(database)
    migrate.run_migrations(pool)
    project = _project(pool)
    fields = _preparation(project)
    _insert_preparation(pool, fields)
    with pool.transaction() as conn:
        AcProjectPreparationRepo.confirm_agent(conn, project["project_id"], 1, updated_at=2000)
        AcProjectPreparationRepo.fail(
            conn, project["project_id"], 1, "providerUnavailable", True, updated_at=3000
        )
    before = AcProjectPreparationRepo(pool).get(project["project_id"], 1)
    pool.close()
    reopened = SqlitePool(database)
    try:
        migrate.run_migrations(reopened)
        services = build_shared_services(
            db=reopened, paths=PathLayout(tmp_path), config=OctopConfig()
        )
        assert services.ac_project_preparation_repo is services.repos.ac_project_preparation_repo
        assert services.ac_project_preparation_repo.get(project["project_id"], 1) == before
        assert services.ac_project_preparation_repo.get_by_project(project["project_id"], 2) is None
    finally:
        reopened.close()


def test_sqlite_and_postgresql_v22_declared_columns_and_constraints_match() -> None:
    sqlite = SQLITE_DDL.read_text(encoding="utf-8")
    postgres = POSTGRES_DDL.read_text(encoding="utf-8")
    pattern = r"CREATE TABLE IF NOT EXISTS ac_project_preparations \((.*?)\n\);"
    sqlite_table = re.search(pattern, sqlite, re.DOTALL)
    pg_table = re.search(pattern, postgres, re.DOTALL)
    assert sqlite_table is not None and pg_table is not None

    def normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text.replace("BIGINT", "INTEGER")).strip()

    sqlite_columns = re.findall(r"^  (\w+) (INTEGER|TEXT)\b", sqlite_table[1], re.MULTILINE)
    pg_columns = re.findall(r"^  (\w+) (INTEGER|BIGINT|TEXT)\b", pg_table[1], re.MULTILINE)
    assert sqlite_columns == [
        (name, "INTEGER" if kind == "BIGINT" else kind) for name, kind in pg_columns
    ]
    assert "id INTEGER PRIMARY KEY AUTOINCREMENT" in sqlite_table[1]
    assert "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" in pg_table[1]
    assert normalize(sqlite_table[1].split("\n", 2)[2]) == normalize(pg_table[1].split("\n", 2)[2])
    for ddl in (sqlite, postgres):
        assert "managed_project_id TEXT REFERENCES ac_projects(project_id)" in ddl
        assert "(managed_by IS NULL AND managed_project_id IS NULL)" in ddl
        assert (
            "managed_by IS NOT NULL AND managed_by = 'agentcomputer' AND managed_project_id IS NOT NULL"
            in ddl
        )
        assert "name TEXT NOT NULL DEFAULT ''" in ddl
