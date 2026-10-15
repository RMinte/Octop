"""Schema v25 pause/cancellation DDL and SQL-only pause invariants."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.unit.db.test_agentcomputer_dispatch import (
    MIGRATIONS,
    _claim,
    _graph,
    _id,
    _legacy_claim,
    _users,
    _version,
)

from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.ac_run_pauses import AcRunPauseRepo

SQLITE_DDL = MIGRATIONS / "025_agentcomputer_pauses.sql"
POSTGRES_DDL = MIGRATIONS / "025_agentcomputer_pauses.pg.sql"


def _v24_pool(path: Path) -> SqlitePool:
    pool = SqlitePool(path)
    with pool.connect() as conn:
        for version, source in migrate._discover("sqlite"):
            if version <= 24:
                conn.executescript(source.read_text(encoding="utf-8"))
        _users(conn)
    assert _version(pool) == 24
    return pool


def _pause(conn, graph, index=1, *, kind="approval"):
    return AcRunPauseRepo.insert(
        conn,
        pause_id=_id(40, index),
        kind=kind,
        run_id=graph["run_id"],
        turn_id=graph["turn_id"],
        task_id=graph["task_id"],
        project_id=graph["project_id"],
        owner_id=graph["owner_id"],
        dispatch_id=_id(12),
        engine_request_id=_id(14),
        claimant_id=_id(13),
        restore_epoch=3,
        execution_epoch=1,
        intent_json='{"fixed":true}',
        intent_digest="sha256:fixture",
        created_at=5000,
        expires_at=9000,
    )


def test_v24_upgrade_and_repair_preserve_existing_graph(tmp_path: Path) -> None:
    pool = _v24_pool(tmp_path / "v24.db")
    try:
        with pool.transaction() as conn:
            graph = _graph(conn)
            _legacy_claim(conn, graph)
            before = tuple(
                tuple(row) for row in conn.execute("SELECT * FROM ac_run_dispatches ORDER BY id")
            )
        migrate.run_migrations(pool)
        migrate.run_migrations(pool)
        assert _version(pool) == 32
        with pool.connect() as conn:
            assert (
                tuple(
                    tuple(row)
                    for row in conn.execute("SELECT * FROM ac_run_dispatches ORDER BY id")
                )
                == before
            )
            assert conn.execute("SELECT COUNT(*) FROM ac_run_pauses").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM ac_run_cancellations").fetchone()[0] == 0
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with pool.transaction() as conn:
            conn.execute("DROP TABLE ac_run_pauses")
            conn.execute("UPDATE _schema_version SET version=999")
        migrate.run_migrations(pool)
        assert _version(pool) == 32
        with pool.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM ac_run_pauses").fetchone()[0] == 0
    finally:
        pool.close()


def test_partial_pause_table_refuses_automatic_reconstruction(tmp_path: Path) -> None:
    pool = _v24_pool(tmp_path / "partial.db")
    try:
        with pool.transaction() as conn:
            conn.execute("CREATE TABLE ac_run_pauses(id INTEGER PRIMARY KEY, pause_id TEXT)")
            conn.execute("INSERT INTO ac_run_pauses VALUES(1,?)", (_id(40),))
        with pytest.raises(RuntimeError, match="Incomplete AgentComputer ac_run_pauses"):
            migrate.run_migrations(pool)
        assert _version(pool) == 24
        with pool.connect() as conn:
            assert tuple(conn.execute("SELECT * FROM ac_run_pauses").fetchone()) == (1, _id(40))
    finally:
        pool.close()


def test_v25_ddl_failure_rolls_back_both_tables(tmp_path: Path, monkeypatch) -> None:
    pool = _v24_pool(tmp_path / "rollback.db")
    broken = tmp_path / "025_broken.sql"
    broken.write_text(
        SQLITE_DDL.read_text(encoding="utf-8").replace(
            "UPDATE _schema_version SET version = 25;",
            "INSERT INTO missing_pause_table VALUES(1);\nUPDATE _schema_version SET version = 25;",
        ),
        encoding="utf-8",
    )
    original = migrate._discover
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                migrate,
                "_discover",
                lambda dialect: [(v, broken if v == 25 else p) for v, p in original(dialect)],
            )
            with pytest.raises(sqlite3.OperationalError, match="missing_pause_table"):
                migrate.run_migrations(pool)
        assert _version(pool) == 24
        with pool.connect() as conn:
            assert (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name IN "
                    "('ac_run_pauses','ac_run_cancellations')"
                ).fetchall()
                == []
            )
    finally:
        pool.close()


def test_one_active_approval_or_question_per_run_and_owned_reads(tmp_path: Path) -> None:
    pool = SqlitePool(tmp_path / "unique.db")
    try:
        migrate.run_migrations(pool)
        with pool.transaction() as conn:
            _users(conn)
            graph = _graph(conn)
            _claim(conn, graph)
            first = _pause(conn, graph)
            assert first is not None and first.phase == "pending"
            assert _pause(conn, graph, index=2, kind="question") is None
            assert AcRunPauseRepo.get_in_transaction(conn, first.pause_id, 2) is None
            assert AcRunPauseRepo.get_in_transaction(conn, first.pause_id, 1) == first
            assert AcRunPauseRepo.get_by_run_in_transaction(conn, graph["run_id"], 1) == first
            expired = AcRunPauseRepo.mark_expired(
                conn, first.pause_id, 1, first.revision, updated_at=6000
            )
            assert expired is not None and expired.phase == "expired"
            second = _pause(conn, graph, index=2, kind="question")
            assert second is not None
            assert AcRunPauseRepo.get_by_run_in_transaction(conn, graph["run_id"], 1) == second
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


def test_paired_sql_contains_same_pause_and_cancel_constraints() -> None:
    for path in (SQLITE_DDL, POSTGRES_DDL):
        text = path.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS ac_run_pauses" in text
        assert "CREATE TABLE IF NOT EXISTS ac_run_cancellations" in text
        assert "idx_ac_run_pauses_one_active_per_run" in text
        assert "idx_ac_run_pauses_decision_key" in text
        assert "phase IN ('pending', 'decided')" in text
