"""Tool-effect SQL invariants; no engine or real PostgreSQL server is involved."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import Barrier
from typing import Any, Literal

import pytest
from tests.unit.db.test_agentcomputer_dispatch import (
    MIGRATIONS,
    _claim,
    _graph,
    _id,
    _users,
    _version,
)

from octop.config import OctopConfig
from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool, _PgConnectionProxy
from octop.infra.db.repos.ac_events import AcEventRepo
from octop.infra.db.repos.ac_runs import AcRunRepo
from octop.infra.db.repos.ac_tasks import AcTaskRepo
from octop.infra.db.repos.ac_tool_effects import (
    AcToolEffectRecoveryCandidate,
    AcToolEffectRepo,
    AcToolEffectRow,
)
from octop.infra.db.services import RepoBundle, build_shared_services
from octop.infra.utils.paths import PathLayout

SQLITE_DDL = MIGRATIONS / "024_agentcomputer_tool_effects.sql"
POSTGRES_DDL = MIGRATIONS / "024_agentcomputer_tool_effects.pg.sql"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[SqlitePool]:
    pool = SqlitePool(tmp_path / "effects.db")
    try:
        migrate.run_migrations(pool)
        with pool.transaction() as conn:
            _users(conn)
        yield pool
    finally:
        pool.close()


def _v23_pool(path: Path) -> SqlitePool:
    pool = SqlitePool(path)
    with pool.connect() as conn:
        for version, source in migrate._discover("sqlite"):
            if version <= 23:
                conn.executescript(source.read_text(encoding="utf-8"))
        _users(conn)
    assert _version(pool) == 23
    return pool


def _fields(graph: dict[str, Any], index: int = 1) -> dict[str, Any]:
    return {
        **graph,
        "effect_id": _id(30, index),
        "dispatch_id": _id(12, index),
        "engine_request_id": _id(14, index),
        "claimant_id": _id(13, index),
        "restore_epoch": 3,
        "execution_epoch": 1,
        "tool_call_id": f"call-{index}",
        "execution_attempt": 1,
        "intent_json": '{ "original": "固定工具意图" }',
        "intent_digest": "sha256:fixed-tool-intent",
        "created_at": 5000,
        "updated_at": 5000,
    }


def _effect(conn: Any, index: int = 1, *, owner_id: int = 1) -> AcToolEffectRow:
    graph = _graph(conn, index, owner_id=owner_id)
    _claim(conn, graph, index)
    row = AcToolEffectRepo.insert(conn, **_fields(graph, index))
    assert row is not None
    return row


def _terminal() -> dict[str, Any]:
    return {
        "terminal_json": '{ "outcome": "notDispatched", "original": true }',
        "terminal_digest": "sha256:original-terminal",
        "terminal_at": 6000,
        "updated_at": 6000,
    }


def _facts(conn: Any) -> dict[str, list[dict[str, Any]]]:
    return {
        table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in (
            "users",
            "ac_projects",
            "ac_tasks",
            "ac_turns",
            "ac_runs",
            "ac_commands",
            "ac_events",
            "ac_run_dispatches",
        )
    }


def test_v23_upgrade_preserves_all_original_facts(tmp_path: Path) -> None:
    pool = _v23_pool(tmp_path / "v23.db")
    try:
        with pool.transaction() as conn:
            graph = _graph(conn)
            _claim(conn, graph)
            before = _facts(conn)
        migrate.run_migrations(pool)
        migrate.run_migrations(pool)
        assert _version(pool) == 25
        with pool.connect() as conn:
            assert _facts(conn) == before
            assert conn.execute("SELECT COUNT(*) FROM ac_tool_effects").fetchone()[0] == 0
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


@pytest.mark.parametrize("watermark", [23, 24, 999])
def test_watermark_repair_does_not_invent_effects(tmp_path: Path, watermark: int) -> None:
    pool = _v23_pool(tmp_path / "repair.db")
    try:
        with pool.transaction() as conn:
            graph = _graph(conn)
            _claim(conn, graph)
            before = _facts(conn)
            conn.execute("UPDATE _schema_version SET version = ?", (watermark,))
        migrate.run_migrations(pool)
        assert _version(pool) == 25
        with pool.connect() as conn:
            assert _facts(conn) == before
            assert conn.execute("SELECT COUNT(*) FROM ac_tool_effects").fetchone()[0] == 0
    finally:
        pool.close()


def test_legacy_v8_user_survives_effect_migration(tmp_path: Path) -> None:
    pool = SqlitePool(tmp_path / "legacy.db")
    try:
        with pool.connect() as conn:
            conn.executescript(
                "CREATE TABLE _schema_version(version INTEGER NOT NULL);"
                "INSERT INTO _schema_version VALUES(8);"
                "CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,"
                "password_hash TEXT NOT NULL,role TEXT NOT NULL,display_name TEXT,"
                "disabled INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL);"
                "INSERT INTO users VALUES(1,'legacy','original','user','Legacy',0,1);"
            )
        migrate.run_migrations(pool)
        assert _version(pool) == 25
        with pool.connect() as conn:
            assert (
                conn.execute("SELECT password_hash FROM users WHERE id=1").fetchone()[0]
                == "original"
            )
            assert conn.execute("SELECT COUNT(*) FROM ac_tool_effects").fetchone()[0] == 0
    finally:
        pool.close()


def test_partial_effect_table_fails_without_repairing_facts(tmp_path: Path) -> None:
    pool = _v23_pool(tmp_path / "partial.db")
    try:
        with pool.transaction() as conn:
            conn.execute("CREATE TABLE ac_tool_effects(id INTEGER PRIMARY KEY, effect_id TEXT)")
            conn.execute("INSERT INTO ac_tool_effects VALUES(1,?)", (_id(30),))
        with pytest.raises(RuntimeError, match="Incomplete AgentComputer tool-effect"):
            migrate.run_migrations(pool)
        assert _version(pool) == 23
        with pool.connect() as conn:
            assert tuple(conn.execute("SELECT * FROM ac_tool_effects").fetchone()) == (1, _id(30))
    finally:
        pool.close()


def test_v24_ddl_failure_rolls_back_table_indexes_and_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _v23_pool(tmp_path / "rollback.db")
    broken = tmp_path / "024_broken.sql"
    broken.write_text(
        SQLITE_DDL.read_text(encoding="utf-8").replace(
            "UPDATE _schema_version SET version = 24;",
            "INSERT INTO missing_effect_table VALUES(1);\nUPDATE _schema_version SET version = 24;",
        ),
        encoding="utf-8",
    )
    original = migrate._discover
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                migrate,
                "_discover",
                lambda dialect: [(v, broken if v == 24 else p) for v, p in original(dialect)],
            )
            with pytest.raises(sqlite3.OperationalError, match="missing_effect_table"):
                migrate.run_migrations(pool)
        assert _version(pool) == 23
        with pool.connect() as conn:
            assert not conn.in_transaction
            assert (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name IN "
                    "('ac_tool_effects','idx_ac_run_dispatches_effect_binding')"
                ).fetchall()
                == []
            )
        migrate.run_migrations(pool)
        assert _version(pool) == 25
    finally:
        pool.close()


@pytest.mark.parametrize(
    "name",
    [
        "idx_ac_run_dispatches_effect_binding",
        "idx_ac_tool_effects_one_pending_per_run",
    ],
)
def test_missing_index_repair_preserves_existing_effect(db: SqlitePool, name: str) -> None:
    with db.transaction() as conn:
        row = _effect(conn)
        conn.execute(f"DROP INDEX {name}")
    migrate.run_migrations(db)
    assert AcToolEffectRepo(db).get(row.effect_id, 1) == row
    with db.connect() as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name=?", (name,)).fetchone()


def test_owned_reads_and_bundle_use_the_same_pool(db: SqlitePool, tmp_path: Path) -> None:
    with db.transaction() as conn:
        row = _effect(conn)
        assert AcToolEffectRepo.get_in_transaction(conn, row.effect_id, 2) is None
        assert AcToolEffectRepo.get_in_transaction(conn, row.effect_id, 1) == row
        assert (
            AcToolEffectRepo.get_by_call_in_transaction(conn, row.run_id, 1, row.tool_call_id, 1)
            == row
        )
        assert (
            AcToolEffectRepo.get_by_call_in_transaction(conn, row.run_id, 2, row.tool_call_id, 1)
            is None
        )
        assert AcToolEffectRepo.list_for_run_in_transaction(conn, row.run_id, 1) == [row]
        assert AcToolEffectRepo.list_for_run_in_transaction(conn, row.run_id, 2) == []
        assert AcToolEffectRepo.get_pending_in_transaction(conn, row.run_id, 1) == row
        assert AcToolEffectRepo.get_pending_in_transaction(conn, row.run_id, 2) is None
    bundle = RepoBundle.from_pool(db)
    services = build_shared_services(paths=PathLayout(tmp_path), config=OctopConfig(), db=db)
    assert bundle.ac_tool_effect_repo.get(row.effect_id, 1) == row
    assert services.ac_tool_effect_repo is services.repos.ac_tool_effect_repo
    assert services.ac_tool_effect_repo.get(row.effect_id, 1) == row
    assert row.id == row.effect_id and type(row.pk) is int


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", _id(4, 2)),
        ("turn_id", _id(3, 2)),
        ("task_id", _id(2, 2)),
        ("project_id", _id(1, 2)),
        ("owner_id", 2),
        ("dispatch_id", _id(12, 2)),
        ("claimant_id", _id(13, 2)),
        ("engine_request_id", _id(14, 2)),
        ("restore_epoch", 4),
        ("execution_epoch", 2),
    ],
)
def test_full_dispatch_binding_rejects_wrong_scope(db: SqlitePool, field: str, value: Any) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        _claim(conn, graph)
        other = _graph(conn, 2, owner_id=2)
        _claim(conn, other, 2)
    fields = _fields(graph)
    fields[field] = value
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), db.transaction() as conn:
        AcToolEffectRepo.insert(conn, **fields)


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_attempt", 2),
        ("execution_attempt", 1.5),
        ("owner_id", 1.5),
        ("restore_epoch", 3.5),
        ("execution_epoch", 1.5),
        ("created_at", 5000.5),
        ("updated_at", -1),
    ],
)
def test_new_journal_rejects_invalid_numeric_values(db: SqlitePool, field: str, value: Any) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        _claim(conn, graph)
    fields = _fields(graph)
    fields[field] = value
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), db.transaction() as conn:
        AcToolEffectRepo.insert(conn, **fields)


def test_duplicate_effect_or_call_never_overwrites_intent(db: SqlitePool) -> None:
    with db.transaction() as conn:
        row = _effect(conn)
        fields = _fields(
            {
                key: getattr(row, key)
                for key in (
                    "run_id",
                    "turn_id",
                    "task_id",
                    "project_id",
                    "owner_id",
                )
            }
        )
        fields["intent_json"] = '{"changed":true}'
        assert AcToolEffectRepo.insert(conn, **fields) is None
        fields["effect_id"] = _id(30, 2)
        assert AcToolEffectRepo.insert(conn, **fields) is None
        assert AcToolEffectRepo.get_in_transaction(conn, row.effect_id, 1) == row
        assert AcToolEffectRepo.get_in_transaction(conn, _id(30, 2), 1) is None


def test_cross_owner_effect_id_collision_preserves_the_original(db: SqlitePool) -> None:
    with db.transaction() as conn:
        original = _effect(conn)
        graph = _graph(conn, 2, owner_id=2)
        _claim(conn, graph, 2)
        fields = _fields(graph, 2)
        fields["effect_id"] = original.effect_id
        assert AcToolEffectRepo.insert(conn, **fields) is None
        assert AcToolEffectRepo.get_in_transaction(conn, original.effect_id, 2) is None
        assert AcToolEffectRepo.get_in_transaction(conn, original.effect_id, 1) == original
        assert AcToolEffectRepo.get_pending_in_transaction(conn, graph["run_id"], 2) is None


@pytest.mark.parametrize("phase", ["prepared", "dispatching", "outcomeUnknown"])
def test_one_unsettled_effect_per_run_blocks_another_call(db: SqlitePool, phase: str) -> None:
    with db.transaction() as conn:
        row = _effect(conn)
        if phase == "dispatching":
            assert AcToolEffectRepo.mark_dispatching(conn, row.effect_id, 1, 1, updated_at=5500)
        elif phase == "outcomeUnknown":
            assert AcToolEffectRepo.mark_outcome_unknown(conn, row.effect_id, 1, 1, updated_at=5500)
        fields = _fields(
            {
                key: getattr(row, key)
                for key in (
                    "run_id",
                    "turn_id",
                    "task_id",
                    "project_id",
                    "owner_id",
                )
            }
        )
        fields.update(effect_id=_id(30, 2), tool_call_id="other-call")
        assert AcToolEffectRepo.insert(conn, **fields) is None
        assert len(AcToolEffectRepo.list_for_run_in_transaction(conn, row.run_id, 1)) == 1


@pytest.mark.parametrize("expected_phase", ["prepared", "dispatching"])
def test_terminal_cas_is_irreversible_and_frees_only_effect_slot(
    db: SqlitePool, expected_phase: Literal["prepared", "dispatching"]
) -> None:
    with db.transaction() as conn:
        row = _effect(conn)
        revision = 1
        if expected_phase == "dispatching":
            current = AcToolEffectRepo.mark_dispatching(conn, row.effect_id, 1, 1, updated_at=5500)
            assert current is not None
            revision = current.revision
        assert (
            AcToolEffectRepo.record_terminal(
                conn, row.effect_id, 2, revision, expected_phase=expected_phase, **_terminal()
            )
            is None
        )
        terminal = AcToolEffectRepo.record_terminal(
            conn, row.effect_id, 1, revision, expected_phase=expected_phase, **_terminal()
        )
        assert (
            terminal is not None
            and terminal.phase == "terminal"
            and terminal.revision == revision + 1
        )
        assert terminal.intent_json == row.intent_json
        assert (
            AcToolEffectRepo.record_terminal(
                conn,
                row.effect_id,
                1,
                terminal.revision,
                expected_phase=expected_phase,
                **_terminal(),
            )
            is None
        )
        assert (
            AcToolEffectRepo.mark_outcome_unknown(
                conn, row.effect_id, 1, terminal.revision, updated_at=7000
            )
            is None
        )
        assert AcToolEffectRepo.get_pending_in_transaction(conn, row.run_id, 1) is None
        assert AcRunRepo.get_in_transaction(conn, row.run_id, 1).state == "queued"
        fields = _fields(
            {
                key: getattr(row, key)
                for key in (
                    "run_id",
                    "turn_id",
                    "task_id",
                    "project_id",
                    "owner_id",
                )
            }
        )
        fields.update(effect_id=_id(30, 2), tool_call_id="next-call")
        assert AcToolEffectRepo.insert(conn, **fields) is not None


@pytest.mark.parametrize("start", [False, True])
def test_unknown_rejects_late_terminal_and_dispatch(db: SqlitePool, start: bool) -> None:
    with db.transaction() as conn:
        row = _effect(conn)
        revision = 1
        if start:
            current = AcToolEffectRepo.mark_dispatching(conn, row.effect_id, 1, 1, updated_at=5500)
            assert current is not None
            revision = current.revision
        unknown = AcToolEffectRepo.mark_outcome_unknown(
            conn, row.effect_id, 1, revision, updated_at=6000
        )
        assert unknown is not None and unknown.phase == "outcomeUnknown"
        for phase in ("prepared", "dispatching"):
            assert (
                AcToolEffectRepo.record_terminal(
                    conn, row.effect_id, 1, unknown.revision, expected_phase=phase, **_terminal()
                )
                is None
            )
        assert (
            AcToolEffectRepo.mark_dispatching(
                conn, row.effect_id, 1, unknown.revision, updated_at=7000
            )
            is None
        )
        assert (
            AcToolEffectRepo.mark_outcome_unknown(
                conn, row.effect_id, 1, unknown.revision, updated_at=7000
            )
            is None
        )
        assert AcToolEffectRepo.get_in_transaction(conn, row.effect_id, 1) == unknown


@pytest.mark.parametrize(
    "mutation",
    [
        "terminal_json='{}'",
        "terminal_digest='x'",
        "terminal_at=6000",
        "phase='terminal'",
        "phase='dispatching'",
        "dispatch_started_at=5500",
    ],
)
def test_phase_and_terminal_group_cannot_be_partial(db: SqlitePool, mutation: str) -> None:
    with db.transaction() as conn:
        row = _effect(conn)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), db.transaction() as conn:
        conn.execute(f"UPDATE ac_tool_effects SET {mutation} WHERE effect_id=?", (row.effect_id,))


def test_effect_and_event_failure_roll_back_together(db: SqlitePool) -> None:
    with db.transaction() as conn:
        row = _effect(conn)
        task = AcTaskRepo.get_in_transaction(conn, row.task_id, 1)
        assert task is not None
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), db.transaction() as conn:
        assert AcToolEffectRepo.mark_dispatching(conn, row.effect_id, 1, 1, updated_at=5500)
        allocation = AcTaskRepo.allocate_event_sequence(
            conn, row.task_id, 1, row.run_id, expected_revision=task.revision, updated_at=5500
        )
        assert allocation is not None
        AcEventRepo.insert(
            conn,
            event_id=_id(7),
            task_id=row.task_id,
            owner_id=1,
            sequence=allocation[0],
            run_id=row.run_id,
            turn_id=row.turn_id,
            event_type="tool.started",
            payload_json="{}",
            created_at=5500,
        )
    with db.connect() as conn:
        assert AcToolEffectRepo.get_in_transaction(conn, row.effect_id, 1) == row
        assert AcTaskRepo.get_in_transaction(conn, row.task_id, 1) == task
        assert len(AcEventRepo.list_for_run_in_transaction(conn, row.run_id, 1)) == 1


def test_concurrent_cas_has_one_winner_across_connections(db: SqlitePool) -> None:
    with db.transaction() as conn:
        row = _effect(conn)
    barrier = Barrier(2)

    def start(pool: SqlitePool) -> bool:
        barrier.wait(timeout=5)
        with pool.transaction() as conn:
            return (
                AcToolEffectRepo.mark_dispatching(conn, row.effect_id, 1, 1, updated_at=5500)
                is not None
            )

    other = SqlitePool(db.path)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(start, pool) for pool in (db, other)]
            assert sorted(f.result(timeout=10) for f in futures) == [False, True]
    finally:
        other.close()
    current = AcToolEffectRepo(db).get(row.effect_id, 1)
    assert current is not None and current.revision == 2 and current.dispatch_started_at == 5500


def test_raw_decoder_rejects_numeric_coercion(db: SqlitePool) -> None:
    with db.transaction() as conn:
        row = _effect(conn)
    values = asdict(row)
    values["id"] = values.pop("pk")
    for field in (
        "id",
        "owner_id",
        "restore_epoch",
        "execution_epoch",
        "execution_attempt",
        "schema_version",
        "revision",
        "created_at",
        "updated_at",
        "dispatch_started_at",
        "terminal_at",
    ):
        for bad in (True, 1.5, "1"):
            with pytest.raises(ValueError, match="integer"):
                AcToolEffectRow.from_row({**values, field: bad})


def test_recovery_fixed_watermark_box_owner_scope_and_interrupted_runs(db: SqlitePool) -> None:
    with db.transaction() as conn:
        rows = [_effect(conn, index, owner_id=1 if index % 2 else 2) for index in range(1, 106)]
        conn.execute("UPDATE users SET disabled=1")
        conn.execute("UPDATE ac_runs SET state='interrupted', reconciliation_required=1")
        through = AcToolEffectRepo.recovery_high_watermark(conn)
        later = _effect(conn, 106)
        first = AcToolEffectRepo.list_recovery_candidates(
            conn, _id(9), after_pk=0, through_pk=through
        )
        second = AcToolEffectRepo.list_recovery_candidates(
            conn, _id(9), after_pk=first[-1].pk, through_pk=through
        )
        assert len(first) == 100 and len(second) == 5
        assert [candidate.effect_id for candidate in first + second] == [
            row.effect_id for row in rows
        ]
        assert later.effect_id not in {candidate.effect_id for candidate in first + second}
        assert {candidate.owner_id for candidate in first} == {1, 2}
        assert (
            AcToolEffectRepo.list_recovery_candidates(
                conn, _id(9, 2), after_pk=0, through_pk=through
            )
            == []
        )
        assert AcToolEffectRepo.mark_outcome_unknown(conn, rows[0].effect_id, 1, 1, updated_at=6000)
        assert AcToolEffectRepo.record_terminal(
            conn, rows[1].effect_id, 2, 1, expected_phase="prepared", **_terminal()
        )
        remaining = AcToolEffectRepo.list_recovery_candidates(
            conn, _id(9), after_pk=0, through_pk=through
        )
        assert rows[0].effect_id not in {candidate.effect_id for candidate in remaining}
        assert rows[1].effect_id not in {candidate.effect_id for candidate in remaining}


def test_recovery_watermark_excludes_terminal_and_unknown(db: SqlitePool) -> None:
    with db.transaction() as conn:
        assert AcToolEffectRepo.recovery_high_watermark(conn) == 0
        first, second = _effect(conn), _effect(conn, 2)
        assert AcToolEffectRepo.mark_outcome_unknown(conn, first.effect_id, 1, 1, updated_at=6000)
        assert AcToolEffectRepo.record_terminal(
            conn, second.effect_id, 1, 1, expected_phase="prepared", **_terminal()
        )
        assert AcToolEffectRepo.recovery_high_watermark(conn) == 0


@pytest.mark.parametrize(
    "field,bad",
    [
        ("id", 1.5),
        ("id", True),
        ("id", "1"),
        ("id", 0),
        ("owner_id", -1),
        ("owner_id", "1"),
        ("effect_id", None),
        ("effect_id", _id(30).replace("-", "")),
    ],
)
def test_recovery_candidate_rejects_damaged_identity(field: str, bad: Any) -> None:
    with pytest.raises(ValueError):
        AcToolEffectRecoveryCandidate.from_row(
            {"id": 1, "effect_id": _id(30), "owner_id": 1, field: bad}
        )


def test_sqlite_postgresql_ddl_match_invariants() -> None:
    sqlite = SQLITE_DDL.read_text(encoding="utf-8")
    postgres = POSTGRES_DDL.read_text(encoding="utf-8")

    def columns(ddl: str) -> list[str]:
        return re.findall(r"^  (\w+) (?:TEXT|INTEGER|BIGINT)\b", ddl, re.M)

    assert columns(sqlite) == columns(postgres)
    for clause in (
        "UNIQUE (run_id, tool_call_id, execution_attempt)",
        "WHERE phase IN ('prepared', 'dispatching', 'outcomeUnknown')",
        "phase IN ('prepared', 'dispatching', 'terminal', 'outcomeUnknown')",
        "REFERENCES ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, project_id, owner_id,",
        "UPDATE _schema_version SET version = 24;",
    ):
        assert clause in sqlite and clause in postgres
    assert "id INTEGER PRIMARY KEY AUTOINCREMENT" in sqlite
    assert "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" in postgres
    assert "execution_attempt = 1" in sqlite and "execution_attempt = 1" in postgres


class _PgCursor:
    @staticmethod
    def fetchone() -> None:
        return None

    @staticmethod
    def fetchall() -> list[Any]:
        return []


class _PgDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Sequence[Any]]] = []

    def execute(self, sql: str, params: Sequence[Any] | None) -> _PgCursor:
        params = params or ()
        assert "?" not in sql and sql.count("%s") == len(params)
        self.calls.append((sql, params))
        return _PgCursor()


def test_postgresql_placeholder_adaptation_for_all_repo_methods() -> None:
    driver = _PgDriver()
    conn = _PgConnectionProxy(driver)
    assert AcToolEffectRepo.get_in_transaction(conn, _id(30), 1) is None
    assert AcToolEffectRepo.get_by_call_in_transaction(conn, _id(4), 1, "call", 1) is None
    assert AcToolEffectRepo.list_for_run_in_transaction(conn, _id(4), 1) == []
    assert AcToolEffectRepo.get_pending_in_transaction(conn, _id(4), 1) is None
    assert (
        AcToolEffectRepo.insert(
            conn,
            **_fields(
                {
                    "run_id": _id(4),
                    "turn_id": _id(3),
                    "task_id": _id(2),
                    "project_id": _id(1),
                    "owner_id": 1,
                }
            ),
        )
        is None
    )
    assert AcToolEffectRepo.mark_dispatching(conn, _id(30), 1, 1, updated_at=5500) is None
    assert (
        AcToolEffectRepo.record_terminal(
            conn, _id(30), 1, 1, expected_phase="prepared", **_terminal()
        )
        is None
    )
    assert AcToolEffectRepo.mark_outcome_unknown(conn, _id(30), 1, 1, updated_at=6000) is None
    assert AcToolEffectRepo.recovery_high_watermark(conn) == 0
    assert AcToolEffectRepo.list_recovery_candidates(conn, _id(9), after_pk=0, through_pk=100) == []
    assert len(driver.calls) == 10
    assert "AND owner_id = %s" in driver.calls[0][0]
    assert "AND revision = %s AND phase = %s" in driver.calls[6][0]
    assert "p.box_id = %s AND e.id > %s AND e.id <= %s" in driver.calls[-1][0]
