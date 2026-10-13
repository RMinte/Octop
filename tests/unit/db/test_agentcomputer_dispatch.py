"""Dispatch journal invariants without an engine or real PostgreSQL server."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from octop.config import OctopConfig
from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool, _PgConnectionProxy
from octop.infra.db.repos._base import strict_bool_int, strict_int
from octop.infra.db.repos.ac_commands import AcCommandRepo, AcCommandRow
from octop.infra.db.repos.ac_events import AcEventRepo, AcEventRow
from octop.infra.db.repos.ac_project_preparations import (
    AcProjectPreparationRepo,
    AcProjectPreparationRow,
)
from octop.infra.db.repos.ac_projects import AcProjectRepo, AcProjectRow
from octop.infra.db.repos.ac_run_dispatches import AcRunDispatchRepo, AcRunDispatchRow
from octop.infra.db.repos.ac_runs import AcRunRepo, AcRunRow
from octop.infra.db.repos.ac_tasks import AcTaskRepo, AcTaskRow
from octop.infra.db.repos.ac_turns import AcTurnRepo, AcTurnRow
from octop.infra.db.services import RepoBundle, build_shared_services
from octop.infra.utils.paths import PathLayout

MIGRATIONS = Path(__file__).resolve().parents[3] / "src/octop/infra/db/migrations"
SQLITE_DDL = MIGRATIONS / "023_agentcomputer_dispatch.sql"
POSTGRES_DDL = MIGRATIONS / "023_agentcomputer_dispatch.pg.sql"


def _id(kind: int, index: int = 1) -> str:
    return f"{kind:08x}-0000-4000-8000-{index:012x}"


def _version(pool: SqlitePool) -> int:
    with pool.connect() as conn:
        return int(conn.execute("SELECT version FROM _schema_version").fetchone()[0])


def _users(conn: Any) -> None:
    for owner_id in (1, 2):
        conn.execute(
            "INSERT INTO users(id,username,password_hash,role,created_at) VALUES(?,?,?,'user',1)",
            (owner_id, f"owner-{owner_id}", "fixture"),
        )


@pytest.fixture
def db(tmp_path: Path) -> Iterator[SqlitePool]:
    pool = SqlitePool(tmp_path / "dispatch.db")
    try:
        migrate.run_migrations(pool)
        with pool.transaction() as conn:
            _users(conn)
        yield pool
    finally:
        pool.close()


def _v22_pool(path: Path) -> SqlitePool:
    pool = SqlitePool(path)
    with pool.connect() as conn:
        for version, source in migrate._discover("sqlite"):
            if version <= 22:
                conn.executescript(source.read_text(encoding="utf-8"))
        _users(conn)
    assert _version(pool) == 22
    return pool


def _graph(conn: Any, index: int = 1, *, owner_id: int = 1) -> dict[str, Any]:
    graph = {
        "project_id": _id(1, index),
        "task_id": _id(2, index),
        "turn_id": _id(3, index),
        "run_id": _id(4, index),
        "owner_id": owner_id,
    }
    assert (
        AcProjectRepo.insert(
            conn,
            project_id=graph["project_id"],
            owner_id=owner_id,
            box_id=_id(9),
            workspace_id=_id(10, index),
            name="DB fixture",
            config_json='{ "instructions": "原配置" }',
            config_digest="sha256:configuration",
            created_at=1000,
            updated_at=1000,
        )
        is not None
    )
    AcProjectRepo.set_preparation(conn, graph["project_id"], owner_id, "ready", updated_at=1000)
    assert (
        AcTaskRepo.insert(
            conn,
            task_id=graph["task_id"],
            project_id=graph["project_id"],
            owner_id=owner_id,
            session_id=_id(8, index),
            octop_thread_id=None,
            title="SQL fixture only",
            preparation_state="ready",
            created_at=1000,
            updated_at=1000,
        )
        is not None
    )
    AcTurnRepo.insert(
        conn,
        turn_id=graph["turn_id"],
        task_id=graph["task_id"],
        project_id=graph["project_id"],
        owner_id=owner_id,
        user_message_id=_id(5, index),
        assistant_message_id=_id(6, index),
        snapshot_json='{ "original": "原快照" }',
        snapshot_digest="sha256:snapshot",
        created_at=1000,
    )
    AcRunRepo.insert(
        conn,
        **graph,
        state="queued",
        dispatch_phase="notDispatched",
        restore_epoch=3,
        created_at=1000,
        updated_at=1000,
    )
    assert AcProjectRepo.acquire_run(
        conn, graph["project_id"], owner_id, graph["run_id"], 1, updated_at=1000
    )
    assert AcTaskRepo.acquire_run(
        conn, graph["task_id"], owner_id, graph["run_id"], updated_at=1000
    )
    AcEventRepo.insert(
        conn,
        event_id=_id(7, index),
        task_id=graph["task_id"],
        owner_id=owner_id,
        sequence=1,
        run_id=graph["run_id"],
        turn_id=graph["turn_id"],
        event_type="run.accepted",
        payload_json='{ "runState": "queued", "original": true }',
        created_at=1000,
    )
    conn.execute(
        "INSERT INTO ac_commands(command_id,owner_id,idempotency_key,operation,target_id,"
        "payload_digest,restore_epoch,receipt_json,created_at) "
        "VALUES(?,?,?,'turn.newMessage',?,'sha256:original',3,?,1000)",
        (_id(11, index), owner_id, f"key-{index}", graph["task_id"], '{ "runState": "queued" }'),
    )
    return graph


def _journal_fields(graph: dict[str, Any], index: int = 1) -> dict[str, Any]:
    return {
        **graph,
        "dispatch_id": _id(12, index),
        "execution_epoch": 1,
        "restore_epoch": 3,
        "claimant_id": _id(13, index),
        "engine_request_id": _id(14, index),
        "intent_json": '{ "schemaVersion": 1, "original": "固定意图" }',
        "intent_digest": "sha256:fixed-intent",
        "created_at": 2000,
        "updated_at": 2000,
    }


def _claim(conn: Any, graph: dict[str, Any], index: int = 1) -> AcRunDispatchRow:
    run = AcRunRepo.claim(conn, graph["run_id"], graph["owner_id"], 1, 3, updated_at=2000)
    assert run is not None and run.execution_epoch == 1 and run.revision == 2
    return AcRunDispatchRepo.insert(conn, **_journal_fields(graph, index))


def _start(conn: Any, graph: dict[str, Any]) -> None:
    assert AcRunDispatchRepo.start_dispatch(
        conn, graph["run_id"], graph["owner_id"], 1, updated_at=3000
    )
    run = AcRunRepo.mark_dispatching(
        conn, graph["run_id"], graph["owner_id"], 2, 1, updated_at=3000
    )
    assert run is not None and run.revision == 3 and run.dispatch_phase == "dispatching"


def _receipt(index: int = 1) -> dict[str, Any]:
    return {
        "engine_receipt_id": _id(15, index),
        "engine_receipt_json": '{ "schemaVersion": 1, "accepted": true }',
        "engine_receipt_digest": "sha256:accepted",
        "engine_accepted_at": 4000,
        "updated_at": 4000,
    }


def _facts(conn: Any) -> dict[str, list[dict[str, Any]]]:
    return {
        table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("ac_projects", "ac_tasks", "ac_turns", "ac_runs", "ac_commands", "ac_events")
    }


def _successor(
    conn: Any, graph: dict[str, Any], index: int, *, another_task: bool
) -> dict[str, Any]:
    task_id = graph["task_id"]
    if another_task:
        task_id = _id(2, index)
        assert (
            AcTaskRepo.insert(
                conn,
                task_id=task_id,
                project_id=graph["project_id"],
                owner_id=graph["owner_id"],
                session_id=_id(8, index),
                octop_thread_id=None,
                title="Next SQL Task",
                preparation_state="ready",
                created_at=6000,
                updated_at=6000,
            )
            is not None
        )
    AcTurnRepo.insert(
        conn,
        turn_id=_id(3, index),
        task_id=task_id,
        project_id=graph["project_id"],
        owner_id=graph["owner_id"],
        user_message_id=_id(5, index),
        assistant_message_id=_id(6, index),
        snapshot_json="{}",
        snapshot_digest="sha256:successor",
        created_at=6000,
    )
    return {**graph, "run_id": _id(4, index), "turn_id": _id(3, index), "task_id": task_id}


def test_v22_upgrade_preserves_original_rows_receipt_and_epoch(tmp_path: Path) -> None:
    pool = _v22_pool(tmp_path / "v22.db")
    try:
        with pool.transaction() as conn:
            _graph(conn)
            before = _facts(conn)
        migrate.run_migrations(pool)
        migrate.run_migrations(pool)
        assert _version(pool) == 25
        with pool.connect() as conn:
            assert _facts(conn) == before
            assert conn.execute("SELECT COUNT(*) FROM ac_run_dispatches").fetchone()[0] == 0
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


@pytest.mark.parametrize("watermark", [22, 23, 999])
def test_watermark_repair_creates_journal_without_claiming_runs(
    tmp_path: Path, watermark: int
) -> None:
    pool = _v22_pool(tmp_path / "repair.db")
    try:
        with pool.transaction() as conn:
            _graph(conn)
            before = _facts(conn)
            conn.execute("UPDATE _schema_version SET version = ?", (watermark,))
        migrate.run_migrations(pool)
        assert _version(pool) == 25
        with pool.connect() as conn:
            assert _facts(conn) == before
            assert conn.execute("SELECT COUNT(*) FROM ac_run_dispatches").fetchone()[0] == 0
    finally:
        pool.close()


def test_v23_missing_v22_journal_finishes_at_23_without_data_changes(db: SqlitePool) -> None:
    with db.transaction() as conn:
        _graph(conn)
        before = _facts(conn)
        conn.execute("DROP TABLE ac_project_preparations")
    migrate.run_migrations(db)
    assert _version(db) == 25
    with db.connect() as conn:
        assert _facts(conn) == before
        assert conn.execute("SELECT COUNT(*) FROM ac_project_preparations").fetchone()[0] == 0


def test_missing_dispatch_journal_does_not_invent_prior_claim_facts(db: SqlitePool) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        _claim(conn, graph)
        _start(conn, graph)
        before = _facts(conn)
        conn.execute("DROP TABLE ac_run_dispatches")
    migrate.run_migrations(db)
    with db.connect() as conn:
        assert _facts(conn) == before
        assert conn.execute("SELECT COUNT(*) FROM ac_run_dispatches").fetchone()[0] == 0
        run = AcRunRepo.get_in_transaction(conn, graph["run_id"], 1)
        assert run is not None and run.dispatch_phase == "dispatching"


def test_v23_ddl_failure_rolls_back_journal_and_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _v22_pool(tmp_path / "rollback.db")
    broken = tmp_path / "023_broken.sql"
    broken.write_text(
        SQLITE_DDL.read_text(encoding="utf-8").replace(
            "UPDATE _schema_version SET version = 23;",
            "INSERT INTO missing_dispatch_table VALUES (1);\nUPDATE _schema_version SET version = 23;",
        ),
        encoding="utf-8",
    )
    original = migrate._discover
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                migrate,
                "_discover",
                lambda dialect: [(v, broken if v == 23 else p) for v, p in original(dialect)],
            )
            with pytest.raises(sqlite3.OperationalError, match="missing_dispatch_table"):
                migrate.run_migrations(pool)
        assert _version(pool) == 22
        with pool.connect() as conn:
            assert not conn.in_transaction
            assert (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='ac_run_dispatches'"
                ).fetchone()
                is None
            )
        migrate.run_migrations(pool)
        assert _version(pool) == 25
    finally:
        pool.close()


def test_incomplete_existing_journal_fails_without_defaulting_missing_facts(tmp_path: Path) -> None:
    pool = _v22_pool(tmp_path / "partial.db")
    try:
        with pool.connect() as conn:
            conn.execute("CREATE TABLE ac_run_dispatches(id INTEGER PRIMARY KEY, dispatch_id TEXT)")
            conn.execute("INSERT INTO ac_run_dispatches VALUES(1,'kept-partial-fact')")
        with pytest.raises(RuntimeError, match="Incomplete AgentComputer dispatch journal"):
            migrate.run_migrations(pool)
        assert _version(pool) == 22
        with pool.connect() as conn:
            assert (
                conn.execute("SELECT dispatch_id FROM ac_run_dispatches").fetchone()[0]
                == "kept-partial-fact"
            )
    finally:
        pool.close()


def test_services_expose_owned_journal_repo(db: SqlitePool, tmp_path: Path) -> None:
    bundle = RepoBundle.from_pool(db)
    assert isinstance(bundle.ac_run_dispatch_repo, AcRunDispatchRepo)
    services = build_shared_services(paths=PathLayout(tmp_path), config=OctopConfig(), db=db)
    assert isinstance(services.ac_run_dispatch_repo, AcRunDispatchRepo)
    with db.transaction() as conn:
        graph = _graph(conn)
        original = _claim(conn, graph)
        assert (
            services.ac_run_dispatch_repo.get_in_transaction(conn, graph["run_id"], 1) == original
        )
    assert services.ac_run_dispatch_repo.get(graph["run_id"], 1) == original
    assert services.ac_run_dispatch_repo.get(graph["run_id"], 2) is None
    assert services.ac_run_dispatch_repo.get(_id(4, 99), 1) is None


def test_claim_requires_owner_revision_restore_epoch_and_both_lanes(db: SqlitePool) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        before = _facts(conn)
        for owner, revision, epoch in ((2, 1, 3), (1, 2, 3), (1, 1, 2)):
            assert (
                AcRunRepo.claim(conn, graph["run_id"], owner, revision, epoch, updated_at=2000)
                is None
            )
        assert _facts(conn) == before
        for table, column in (("ac_tasks", "task_id"), ("ac_projects", "project_id")):
            conn.execute(
                f"UPDATE {table} SET active_run_id=NULL WHERE {column}=?", (graph[column],)
            )
            assert AcRunRepo.claim(conn, graph["run_id"], 1, 1, 3, updated_at=2000) is None
            conn.execute(
                f"UPDATE {table} SET active_run_id=? WHERE {column}=?",
                (graph["run_id"], graph[column]),
            )
        original = _claim(conn, graph)
        assert original.execution_epoch == 1 and original.revision == 1
        assert AcRunRepo.claim(conn, graph["run_id"], 1, 2, 3, updated_at=900000) is None
        assert AcRunDispatchRepo.get_in_transaction(conn, graph["run_id"], 1) == original


@pytest.mark.parametrize(
    "column,value",
    [
        ("state", "running"),
        ("state", "interrupted"),
        ("dispatch_phase", "claimed"),
        ("execution_epoch", 1),
        ("reconciliation_required", 1),
    ],
)
def test_claim_does_not_reclaim_other_phases_or_reconciliation(
    db: SqlitePool, column: str, value: object
) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        conn.execute(f"UPDATE ac_runs SET {column}=? WHERE run_id=?", (value, graph["run_id"]))
        assert AcRunRepo.claim(conn, graph["run_id"], 1, 1, 3, updated_at=2000) is None


def test_competing_connections_claim_exactly_once(db: SqlitePool) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
    other = SqlitePool(db.path)
    barrier = Barrier(2)

    def compete(pool: SqlitePool, index: int) -> bool:
        barrier.wait(timeout=5)
        with pool.transaction() as conn:
            run = AcRunRepo.claim(conn, graph["run_id"], 1, 1, 3, updated_at=2000)
            if run is None:
                return False
            AcRunDispatchRepo.insert(conn, **_journal_fields(graph, index))
            return True

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(compete, pool, i) for i, pool in enumerate((db, other), 1)]
            assert sorted(f.result(timeout=10) for f in futures) == [False, True]
        with db.connect() as conn:
            run = AcRunRepo.get_in_transaction(conn, graph["run_id"], 1)
            assert run is not None and run.execution_epoch == 1 and run.revision == 2
            assert conn.execute("SELECT COUNT(*) FROM ac_run_dispatches").fetchone()[0] == 1
    finally:
        other.close()


def test_dispatch_and_acceptance_cas_preserve_fixed_intent_and_queued_state(db: SqlitePool) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        original = _claim(conn, graph)
        assert not AcRunDispatchRepo.start_dispatch(conn, graph["run_id"], 2, 1, updated_at=3000)
        assert not AcRunDispatchRepo.start_dispatch(conn, graph["run_id"], 1, 2, updated_at=3000)
        assert not AcRunDispatchRepo.record_acceptance(conn, graph["run_id"], 1, 1, **_receipt())
        assert (
            AcRunRepo.mark_engine_accepted(conn, graph["run_id"], 1, 2, 1, updated_at=4000) is None
        )
        for owner, revision, epoch in ((2, 2, 1), (1, 1, 1), (1, 2, 2)):
            assert (
                AcRunRepo.mark_dispatching(
                    conn, graph["run_id"], owner, revision, epoch, updated_at=3000
                )
                is None
            )
        _start(conn, graph)
        assert not AcRunDispatchRepo.start_dispatch(conn, graph["run_id"], 1, 2, updated_at=5000)
        assert AcRunRepo.mark_dispatching(conn, graph["run_id"], 1, 3, 1, updated_at=3000) is None
        assert not AcRunDispatchRepo.record_acceptance(conn, graph["run_id"], 2, 2, **_receipt())
        assert not AcRunDispatchRepo.record_acceptance(conn, graph["run_id"], 1, 1, **_receipt())
        for owner, revision, epoch in ((2, 3, 1), (1, 2, 1), (1, 3, 2)):
            assert (
                AcRunRepo.mark_engine_accepted(
                    conn, graph["run_id"], owner, revision, epoch, updated_at=4000
                )
                is None
            )
        assert AcRunDispatchRepo.record_acceptance(conn, graph["run_id"], 1, 2, **_receipt())
        run = AcRunRepo.mark_engine_accepted(conn, graph["run_id"], 1, 3, 1, updated_at=4000)
        assert run is not None and run.state == "queued" and run.dispatch_phase == "engineAccepted"
        accepted = AcRunDispatchRepo.get_in_transaction(conn, graph["run_id"], 1)
        assert (
            accepted is not None and accepted.revision == 3 and accepted.engine_accepted_at == 4000
        )
        for key in (
            "dispatch_id",
            "run_id",
            "turn_id",
            "task_id",
            "project_id",
            "owner_id",
            "execution_epoch",
            "restore_epoch",
            "claimant_id",
            "engine_request_id",
            "intent_json",
            "intent_digest",
            "created_at",
        ):
            assert getattr(accepted, key) == getattr(original, key)
        assert not AcRunDispatchRepo.record_acceptance(conn, graph["run_id"], 1, 3, **_receipt(2))
        assert not AcRunDispatchRepo.record_acceptance(conn, graph["run_id"], 1, 3, **_receipt())
        assert (
            AcRunRepo.mark_engine_accepted(conn, graph["run_id"], 1, 4, 1, updated_at=5000) is None
        )
        assert AcRunDispatchRepo.get_in_transaction(conn, graph["run_id"], 1) == accepted


@pytest.mark.parametrize("phase", ["claimed", "dispatching", "engineAccepted"])
def test_interruption_preserves_phase_epoch_and_blocks_both_lanes(
    db: SqlitePool, phase: str
) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        _claim(conn, graph)
        if phase != "claimed":
            _start(conn, graph)
        if phase == "engineAccepted":
            assert AcRunDispatchRepo.record_acceptance(conn, graph["run_id"], 1, 2, **_receipt())
            assert AcRunRepo.mark_engine_accepted(conn, graph["run_id"], 1, 3, 1, updated_at=4000)
        before = AcRunRepo.get_in_transaction(conn, graph["run_id"], 1)
        assert before is not None
        for owner, revision, epoch in (
            (2, before.revision, 1),
            (1, before.revision + 1, 1),
            (1, before.revision, 2),
        ):
            assert (
                AcRunRepo.mark_interrupted(
                    conn, graph["run_id"], owner, revision, epoch, updated_at=5000
                )
                is None
            )
        run = AcRunRepo.mark_interrupted(
            conn, graph["run_id"], 1, before.revision, 1, updated_at=5000
        )
        assert run is not None and run.state == "interrupted" and run.reconciliation_required
        assert run.dispatch_phase == phase and run.execution_epoch == 1
        assert (
            AcTaskRepo.get_in_transaction(conn, graph["task_id"], 1).active_run_id
            == graph["run_id"]
        )
        assert (
            AcProjectRepo.get_in_transaction(conn, graph["project_id"], 1).active_run_id
            == graph["run_id"]
        )
        assert (
            AcRunRepo.mark_interrupted(conn, graph["run_id"], 1, run.revision, 1, updated_at=6000)
            is None
        )
        assert (
            AcRunRepo.claim(conn, graph["run_id"], 1, run.revision, 3, updated_at=999999999) is None
        )
        for another_task in (False, True):
            successor = _successor(conn, graph, 90 + int(another_task), another_task=another_task)
            conn.execute("SAVEPOINT conflicting_run")
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
                AcRunRepo.insert(
                    conn,
                    **successor,
                    state="queued",
                    dispatch_phase="notDispatched",
                    created_at=6000,
                    updated_at=6000,
                )
            conn.execute("ROLLBACK TO conflicting_run")
            conn.execute("RELEASE conflicting_run")


def test_event_sequence_is_owner_run_and_revision_scoped(db: SqlitePool) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        for owner, run_id, revision in (
            (2, graph["run_id"], 2),
            (1, _id(4, 99), 2),
            (1, graph["run_id"], 1),
        ):
            assert (
                AcTaskRepo.allocate_event_sequence(
                    conn,
                    graph["task_id"],
                    owner,
                    run_id,
                    expected_revision=revision,
                    updated_at=2000,
                )
                is None
            )
        assert AcTaskRepo.allocate_event_sequence(
            conn, graph["task_id"], 1, graph["run_id"], expected_revision=2, updated_at=2000
        ) == (2, 3)
        assert (
            AcTaskRepo.allocate_event_sequence(
                conn, graph["task_id"], 1, graph["run_id"], expected_revision=2, updated_at=3000
            )
            is None
        )
        assert AcTaskRepo.allocate_event_sequence(
            conn, graph["task_id"], 1, graph["run_id"], expected_revision=3, updated_at=3000
        ) == (3, 4)
        assert AcProjectRepo.get_in_transaction(conn, graph["project_id"], 1).revision == 1


def test_event_failure_rolls_back_claim_journal_and_task_sequence(db: SqlitePool) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        before = _facts(conn)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), db.transaction() as conn:
        _claim(conn, graph)
        assert AcTaskRepo.allocate_event_sequence(
            conn, graph["task_id"], 1, graph["run_id"], expected_revision=2, updated_at=2000
        ) == (2, 3)
        AcEventRepo.insert(
            conn,
            event_id=_id(7),
            task_id=graph["task_id"],
            owner_id=1,
            sequence=2,
            run_id=graph["run_id"],
            turn_id=graph["turn_id"],
            event_type="run.claimed",
            payload_json="{}",
            created_at=2000,
        )
    with db.connect() as conn:
        assert _facts(conn) == before
        assert conn.execute("SELECT COUNT(*) FROM ac_run_dispatches").fetchone()[0] == 0
    with db.transaction() as conn:
        _claim(conn, graph)
        sequence = AcTaskRepo.allocate_event_sequence(
            conn, graph["task_id"], 1, graph["run_id"], expected_revision=2, updated_at=2000
        )
        assert sequence == (2, 3)
        AcEventRepo.insert(
            conn,
            event_id=_id(7, 2),
            task_id=graph["task_id"],
            owner_id=1,
            sequence=sequence[0],
            run_id=graph["run_id"],
            turn_id=graph["turn_id"],
            event_type="run.claimed",
            payload_json="{}",
            created_at=2000,
        )


def test_restart_preserves_dispatch_receipt_and_never_requeues(db: SqlitePool) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        _claim(conn, graph)
        _start(conn, graph)
    reopened = SqlitePool(db.path)
    try:
        migrate.run_migrations(reopened)
        with reopened.transaction() as conn:
            journal = AcRunDispatchRepo.get_in_transaction(conn, graph["run_id"], 1)
            assert journal is not None and journal.engine_receipt_id is None
            assert AcRunRepo.claim(conn, graph["run_id"], 1, 3, 3, updated_at=900000) is None
            interrupted = AcRunRepo.mark_interrupted(
                conn, graph["run_id"], 1, 3, 1, updated_at=5000
            )
            assert interrupted is not None and interrupted.reconciliation_required
            assert AcRunDispatchRepo.get_in_transaction(conn, graph["run_id"], 1) == journal
            assert (
                AcRunRepo.mark_engine_accepted(conn, graph["run_id"], 1, 4, 1, updated_at=6000)
                is None
            )
    finally:
        reopened.close()


def test_acceptance_event_failure_rolls_back_receipt_phase_and_sequence(db: SqlitePool) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        _claim(conn, graph)
        _start(conn, graph)
        before = _facts(conn)
        before_journal = AcRunDispatchRepo.get_in_transaction(conn, graph["run_id"], 1)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), db.transaction() as conn:
        assert AcRunDispatchRepo.record_acceptance(conn, graph["run_id"], 1, 2, **_receipt())
        assert AcRunRepo.mark_engine_accepted(conn, graph["run_id"], 1, 3, 1, updated_at=4000)
        assert AcTaskRepo.allocate_event_sequence(
            conn, graph["task_id"], 1, graph["run_id"], expected_revision=2, updated_at=4000
        ) == (2, 3)
        AcEventRepo.insert(
            conn,
            event_id=_id(7),
            task_id=graph["task_id"],
            owner_id=1,
            sequence=2,
            run_id=graph["run_id"],
            turn_id=graph["turn_id"],
            event_type="run.engineAccepted",
            payload_json="{}",
            created_at=4000,
        )
    with db.connect() as conn:
        assert _facts(conn) == before
        assert AcRunDispatchRepo.get_in_transaction(conn, graph["run_id"], 1) == before_journal


@pytest.mark.parametrize("field", ["owner_id", "turn_id", "task_id", "project_id"])
def test_journal_composite_foreign_keys_reject_cross_owner_graph(
    db: SqlitePool, field: str
) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        other = _graph(conn, 2, owner_id=2)
    fields = _journal_fields(graph)
    fields[field] = other[field]
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), db.transaction() as conn:
        AcRunDispatchRepo.insert(conn, **fields)


@pytest.mark.parametrize("field", ["dispatch_id", "run_id", "engine_request_id"])
def test_duplicate_journal_identity_never_overwrites_original(db: SqlitePool, field: str) -> None:
    with db.transaction() as conn:
        first = _graph(conn)
        second = _graph(conn, 2)
        original = _claim(conn, first)
    fields = _journal_fields(second, 2)
    fields[field] = getattr(original, field)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), db.transaction() as conn:
        AcRunDispatchRepo.insert(conn, **fields)
    assert AcRunDispatchRepo(db).get(first["run_id"], 1) == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_epoch", 0),
        ("execution_epoch", 1.5),
        ("restore_epoch", -1),
        ("restore_epoch", "bad"),
        ("schema_version", 1.5),
        ("revision", 0),
        ("created_at", -1),
        ("updated_at", 1.5),
        ("dispatch_started_at", "bad"),
        ("engine_accepted_at", 1.5),
    ],
)
def test_journal_numeric_storage_is_strict(db: SqlitePool, field: str, value: object) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        _claim(conn, graph)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), db.transaction() as conn:
        conn.execute(
            f"UPDATE ac_run_dispatches SET {field}=? WHERE run_id=?", (value, graph["run_id"])
        )


@pytest.mark.parametrize("mask", range(1, 15))
def test_engine_receipt_group_rejects_every_partial_subset(db: SqlitePool, mask: int) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        _claim(conn, graph)
        _start(conn, graph)
    fields = {
        key: value
        for bit, (key, value) in enumerate(_receipt().items())
        if key != "updated_at" and mask & (1 << bit)
    }
    columns = ", ".join(f"{field}=?" for field in fields)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), db.transaction() as conn:
        conn.execute(
            f"UPDATE ac_run_dispatches SET {columns} WHERE run_id=?",
            (*fields.values(), graph["run_id"]),
        )


def test_receipt_requires_dispatch_start_and_global_receipt_identity(db: SqlitePool) -> None:
    with db.transaction() as conn:
        first, second = _graph(conn), _graph(conn, 2)
        _claim(conn, first)
        _claim(conn, second, 2)
    receipt = _receipt()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), db.transaction() as conn:
        conn.execute(
            "UPDATE ac_run_dispatches SET engine_receipt_id=?, engine_receipt_json=?, engine_receipt_digest=?, engine_accepted_at=? WHERE run_id=?",
            (
                receipt["engine_receipt_id"],
                receipt["engine_receipt_json"],
                receipt["engine_receipt_digest"],
                receipt["engine_accepted_at"],
                first["run_id"],
            ),
        )
    with db.transaction() as conn:
        _start(conn, first)
        _start(conn, second)
        assert AcRunDispatchRepo.record_acceptance(conn, first["run_id"], 1, 2, **receipt)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), db.transaction() as conn:
        AcRunDispatchRepo.record_acceptance(conn, second["run_id"], 1, 2, **receipt)


def test_sqlite_postgresql_ddl_have_equal_columns_identity_and_scope_constraints() -> None:
    sqlite = SQLITE_DDL.read_text(encoding="utf-8")
    postgres = POSTGRES_DDL.read_text(encoding="utf-8")
    pattern = r"CREATE TABLE IF NOT EXISTS ac_run_dispatches \((.*?)\n\);"
    sqlite_body, postgres_body = (
        re.search(pattern, text, re.S).group(1) for text in (sqlite, postgres)
    )

    def columns(body: str) -> list[str]:
        return re.findall(r"^  (\w+) (?:TEXT|INTEGER|BIGINT)\b", body, re.M)

    assert columns(sqlite_body) == columns(postgres_body)
    assert "id INTEGER PRIMARY KEY AUTOINCREMENT" in sqlite_body
    assert "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" in postgres_body
    assert "owner_id BIGINT NOT NULL REFERENCES users(id)" in postgres_body
    assert "history_turn_id" not in sqlite and "history_turn_id" not in postgres
    for column in ("dispatch_id", "run_id", "engine_request_id"):
        assert f"{column} TEXT NOT NULL UNIQUE" in sqlite_body
        assert f"{column} TEXT NOT NULL UNIQUE" in postgres_body
    for scope in ("run_id, turn_id, task_id, owner_id", "run_id, project_id, owner_id"):
        clause = f"FOREIGN KEY ({scope})\n    REFERENCES ac_runs({scope})"
        assert clause in sqlite_body and clause in postgres_body
    nullable_group = "(engine_receipt_id IS NULL AND engine_receipt_json IS NULL\n      AND engine_receipt_digest IS NULL AND engine_accepted_at IS NULL)"
    present_group = "(engine_receipt_id IS NOT NULL AND engine_receipt_json IS NOT NULL\n      AND engine_receipt_digest IS NOT NULL AND engine_accepted_at IS NOT NULL\n      AND dispatch_started_at IS NOT NULL)"
    for clause in (nullable_group, present_group):
        assert clause in sqlite_body and clause in postgres_body
    assert "UPDATE _schema_version SET version = 23;" in sqlite
    assert "UPDATE _schema_version SET version = 23;" in postgres


class _PgCursor:
    rowcount = 1

    @staticmethod
    def fetchone() -> None:
        return None


class _PgDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Sequence[Any]]] = []

    def execute(self, sql: str, params: Sequence[Any]) -> _PgCursor:
        assert "?" not in sql and sql.count("%s") == len(params)
        self.calls.append((sql, params))
        return _PgCursor()


def test_postgresql_placeholder_adaptation_for_all_new_repo_operations() -> None:
    driver = _PgDriver()
    conn = _PgConnectionProxy(driver)
    assert AcRunDispatchRepo.get_in_transaction(conn, _id(4), 1) is None
    fields = _journal_fields(
        {
            "run_id": _id(4),
            "turn_id": _id(3),
            "task_id": _id(2),
            "project_id": _id(1),
            "owner_id": 1,
        }
    )
    with pytest.raises(RuntimeError, match="insert returned no row"):
        AcRunDispatchRepo.insert(conn, **fields)
    assert AcRunDispatchRepo.start_dispatch(conn, _id(4), 1, 1, updated_at=3000)
    assert AcRunDispatchRepo.record_acceptance(conn, _id(4), 1, 2, **_receipt())
    assert AcRunRepo.claim(conn, _id(4), 1, 1, 3, updated_at=2000) is None
    for method in (
        AcRunRepo.mark_dispatching,
        AcRunRepo.mark_engine_accepted,
        AcRunRepo.mark_interrupted,
    ):
        assert method(conn, _id(4), 1, 2, 1, updated_at=3000) is None
    assert (
        AcTaskRepo.allocate_event_sequence(
            conn, _id(2), 1, _id(4), expected_revision=2, updated_at=2000
        )
        is None
    )
    assert len(driver.calls) == 9
    assert "AND owner_id = %s" in driver.calls[0][0]
    assert "AND restore_epoch = %s" in driver.calls[4][0]


_NUMERIC_ROWS = [
    (
        "ac_project_preparations",
        AcProjectPreparationRow,
        (
            "id",
            "owner_id",
            "restore_epoch",
            "schema_version",
            "revision",
            "created_at",
            "updated_at",
        ),
    ),
    (
        "ac_projects",
        AcProjectRow,
        ("id", "owner_id", "revision", "schema_version", "created_at", "updated_at"),
    ),
    (
        "ac_tasks",
        AcTaskRow,
        (
            "id",
            "owner_id",
            "revision",
            "schema_version",
            "next_sequence",
            "created_at",
            "updated_at",
        ),
    ),
    ("ac_turns", AcTurnRow, ("id", "owner_id", "schema_version", "created_at")),
    (
        "ac_runs",
        AcRunRow,
        (
            "id",
            "owner_id",
            "execution_epoch",
            "restore_epoch",
            "revision",
            "schema_version",
            "created_at",
            "updated_at",
        ),
    ),
    (
        "ac_commands",
        AcCommandRow,
        ("id", "owner_id", "restore_epoch", "schema_version", "created_at"),
    ),
    (
        "ac_events",
        AcEventRow,
        ("id", "owner_id", "sequence", "payload_version", "schema_version", "created_at"),
    ),
    (
        "ac_run_dispatches",
        AcRunDispatchRow,
        (
            "id",
            "owner_id",
            "execution_epoch",
            "restore_epoch",
            "schema_version",
            "revision",
            "created_at",
            "updated_at",
            "dispatch_started_at",
            "engine_accepted_at",
        ),
    ),
]


@pytest.mark.parametrize(
    "table,decoder,field",
    [
        pytest.param(table, decoder, field, id=f"{table}.{field}")
        for table, decoder, fields in _NUMERIC_ROWS
        for field in fields
    ],
)
def test_control_plane_row_decoders_reject_fractional_numeric_facts(
    db: SqlitePool, table: str, decoder: Any, field: str
) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        AcProjectPreparationRepo.insert(
            conn,
            preparation_id=_id(16),
            project_id=graph["project_id"],
            owner_id=1,
            planned_agent_id="sql-fixture-agent",
            intent_json="{}",
            intent_digest="sha256:preparation",
            restore_epoch=3,
            created_at=1000,
            updated_at=1000,
        )
        _claim(conn, graph)
        _start(conn, graph)
        assert AcRunDispatchRepo.record_acceptance(conn, graph["run_id"], 1, 2, **_receipt())
        raw = dict(conn.execute(f"SELECT * FROM {table}").fetchone())
    decoder.from_row(raw)
    for invalid in (1.5, True, "2"):
        with pytest.raises(ValueError, match="not an integer"):
            decoder.from_row({**raw, field: invalid})


@pytest.mark.parametrize("invalid", [True, 1.5, "1", 2, -1])
def test_run_reconciliation_flag_requires_stored_integer_zero_or_one(
    db: SqlitePool, invalid: object
) -> None:
    with db.transaction() as conn:
        _graph(conn)
        raw = dict(conn.execute("SELECT * FROM ac_runs").fetchone())
    with pytest.raises(ValueError):
        AcRunRow.from_row({**raw, "reconciliation_required": invalid})
    assert not AcRunRow.from_row({**raw, "reconciliation_required": 0}).reconciliation_required
    assert AcRunRow.from_row({**raw, "reconciliation_required": 1}).reconciliation_required


def test_integer_decoders_preserve_real_sqlite_and_postgresql_shaped_ints() -> None:
    assert strict_int(0) == 0 and strict_int(2**53 + 1) == 2**53 + 1
    assert strict_bool_int(1) is True and strict_bool_int(0) is False
    for invalid in (True, False, 1.0, "1", None):
        with pytest.raises(ValueError):
            strict_int(invalid)


@pytest.mark.parametrize("invalid", [True, 1.5, "1", 2, -1])
def test_preparation_retryable_flag_requires_stored_integer_zero_or_one(
    db: SqlitePool, invalid: object
) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        AcProjectPreparationRepo.insert(
            conn,
            preparation_id=_id(16),
            project_id=graph["project_id"],
            owner_id=1,
            planned_agent_id="sql-fixture-agent",
            intent_json="{}",
            intent_digest="sha256:preparation",
            restore_epoch=3,
            created_at=1000,
            updated_at=1000,
        )
        raw = dict(conn.execute("SELECT * FROM ac_project_preparations").fetchone())
    with pytest.raises(ValueError):
        AcProjectPreparationRow.from_row({**raw, "retryable": invalid})
    assert not AcProjectPreparationRow.from_row({**raw, "retryable": 0}).retryable
    assert AcProjectPreparationRow.from_row({**raw, "retryable": 1}).retryable


def test_fractional_execution_epoch_cannot_satisfy_late_worker_cas(db: SqlitePool) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        _claim(conn, graph)
        _start(conn, graph)
        # The old v21 INTEGER-affinity column permits this malformed REAL value.
        conn.execute("UPDATE ac_runs SET execution_epoch=1.5 WHERE run_id=?", (graph["run_id"],))
        before = _facts(conn)
        with pytest.raises(ValueError, match="not an integer"):
            AcRunRepo.get_in_transaction(conn, graph["run_id"], 1)
        assert (
            AcRunRepo.mark_engine_accepted(conn, graph["run_id"], 1, 3, 1, updated_at=9000) is None
        )
        assert AcRunRepo.mark_interrupted(conn, graph["run_id"], 1, 3, 1, updated_at=9000) is None
        assert _facts(conn) == before
        journal = AcRunDispatchRepo.get_in_transaction(conn, graph["run_id"], 1)
        assert journal is not None and journal.engine_receipt_id is None


def test_fractional_task_revision_cannot_allocate_event_sequence(db: SqlitePool) -> None:
    with db.transaction() as conn:
        graph = _graph(conn)
        conn.execute("UPDATE ac_tasks SET revision=2.5 WHERE task_id=?", (graph["task_id"],))
        with pytest.raises(ValueError, match="not an integer"):
            AcTaskRepo.get_in_transaction(conn, graph["task_id"], 1)
        assert (
            AcTaskRepo.allocate_event_sequence(
                conn, graph["task_id"], 1, graph["run_id"], expected_revision=2, updated_at=9000
            )
            is None
        )
        assert conn.execute("SELECT next_sequence FROM ac_tasks").fetchone()[0] == 2


def test_recovery_lock_allows_disabled_owner_to_tighten_existing_run(db: SqlitePool) -> None:
    command_repo, run_repo = AcCommandRepo(db), AcRunRepo(db)
    with db.transaction() as conn:
        graph = _graph(conn)
        _claim(conn, graph)
        _start(conn, graph)
        conn.execute("UPDATE users SET disabled=1 WHERE id=1")
        assert not command_repo.lock_owner(conn, 1)
        assert command_repo.lock_owner_for_recovery(conn, 1)
        assert not command_repo.lock_owner_for_recovery(conn, 99)
        assert run_repo.lock_for_update(conn, graph["run_id"], 1)
        assert not run_repo.lock_for_update(conn, graph["run_id"], 2)
        assert not run_repo.lock_for_update(conn, _id(4, 99), 1)
        run = run_repo.mark_interrupted(conn, graph["run_id"], 1, 3, 1, updated_at=5000)
        assert run is not None and run.state == "interrupted" and run.reconciliation_required
        assert run.dispatch_phase == "dispatching" and run.execution_epoch == 1
        assert (
            AcTaskRepo.get_in_transaction(conn, graph["task_id"], 1).active_run_id
            == graph["run_id"]
        )
        assert (
            AcProjectRepo.get_in_transaction(conn, graph["project_id"], 1).active_run_id
            == graph["run_id"]
        )
        assert AcTaskRepo.allocate_event_sequence(
            conn, graph["task_id"], 1, graph["run_id"], expected_revision=2, updated_at=5000
        ) == (2, 3)
        AcEventRepo.insert(
            conn,
            event_id=_id(7, 2),
            task_id=graph["task_id"],
            owner_id=1,
            sequence=2,
            run_id=graph["run_id"],
            turn_id=graph["turn_id"],
            event_type="run.interrupted",
            payload_json='{"reconciliationRequired":true}',
            created_at=5000,
        )
        assert not command_repo.lock_owner(conn, 1)


def test_postgresql_recovery_lock_sql_preserves_owner_before_run_without_enabled_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _PgDriver()
    conn = _PgConnectionProxy(driver)
    # A SQL-shape fixture, not a running PostgreSQL connection or concurrency proof.
    pool = type("PgShapePool", (), {"dialect": "postgresql"})()
    monkeypatch.setattr(_PgCursor, "fetchone", staticmethod(lambda: {"id": 1}))
    assert AcCommandRepo(pool).lock_owner_for_recovery(conn, 1)
    assert AcRunRepo(pool).lock_for_update(conn, _id(4), 1)
    assert driver.calls == [
        ("SELECT id FROM users WHERE id = %s FOR UPDATE", (1,)),
        ("SELECT run_id FROM ac_runs WHERE run_id = %s AND owner_id = %s FOR UPDATE", (_id(4), 1)),
    ]
    assert "disabled" not in driver.calls[0][0]
