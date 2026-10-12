"""Owned same-connection reads; PostgreSQL checks exercise only placeholder adaptation."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool, _PgConnectionProxy
from octop.infra.db.repos.ac_events import AcEventRepo, AcEventRow
from octop.infra.db.repos.ac_projects import AcProjectRepo
from octop.infra.db.repos.ac_runs import AcRunRepo
from octop.infra.db.repos.ac_tasks import AcTaskRepo
from octop.infra.db.repos.ac_turns import AcTurnRepo, AcTurnRow


def _id(kind: int, index: int) -> str:
    return f"{kind:08x}-0000-4000-8000-{index:012x}"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[SqlitePool]:
    pool = SqlitePool(tmp_path / "reads.db")
    try:
        run_migrations(pool)
        with pool.transaction() as conn:
            conn.execute(
                "INSERT INTO users(id,username,password_hash,role,created_at) "
                "VALUES(1,'one','fixture','user',1)"
            )
            conn.execute(
                "INSERT INTO users(id,username,password_hash,role,created_at) "
                "VALUES(2,'two','fixture','user',1)"
            )
        yield pool
    finally:
        pool.close()


def _graph(conn: Any, index: int, *, owner_id: int = 1) -> tuple[AcTurnRow, str]:
    project_id, task_id, turn_id, run_id = (_id(kind, index) for kind in range(1, 5))
    assert (
        AcProjectRepo.insert(
            conn,
            project_id=project_id,
            owner_id=owner_id,
            box_id=_id(9, 1),
            workspace_id=_id(10, index),
            name="DB read fixture",
            config_json="{}",
            config_digest="sha256:fixture",
            created_at=1000,
            updated_at=1000,
        )
        is not None
    )
    assert (
        AcTaskRepo.insert(
            conn,
            task_id=task_id,
            project_id=project_id,
            owner_id=owner_id,
            session_id=_id(8, index),
            octop_thread_id=None,
            title="DB read fixture",
            preparation_state="pending",
            created_at=1000,
            updated_at=1000,
        )
        is not None
    )
    turn = AcTurnRepo.insert(
        conn,
        turn_id=turn_id,
        task_id=task_id,
        project_id=project_id,
        owner_id=owner_id,
        user_message_id=_id(5, index),
        assistant_message_id=_id(6, index),
        snapshot_json='{ "original": "原始内容", "request": {"prompt":"  exact  "} }',
        snapshot_digest="sha256:original-snapshot",
        created_at=1234,
    )
    AcRunRepo.insert(
        conn,
        run_id=run_id,
        turn_id=turn_id,
        task_id=task_id,
        project_id=project_id,
        owner_id=owner_id,
        state="succeeded",
        dispatch_phase="notDispatched",
        execution_epoch=0,
        restore_epoch=3,
        revision=2,
        created_at=1234,
        updated_at=2345,
    )
    return turn, run_id


def _event(
    conn: Any, turn: AcTurnRow, run_id: str, sequence: int, *, event_type: str = "run.accepted"
) -> AcEventRow:
    return AcEventRepo.insert(
        conn,
        event_id=_id(7, turn.pk * 100 + sequence),
        task_id=turn.task_id,
        owner_id=turn.owner_id,
        sequence=sequence,
        run_id=run_id,
        turn_id=turn.turn_id,
        event_type=event_type,
        payload_json='{ "original": "接收事实", "runState": "queued" }',
        created_at=1234,
    )


def test_owned_turn_read_preserves_original_snapshot_and_sees_current_transaction(
    db: SqlitePool,
) -> None:
    with db.transaction() as conn:
        turn, _run_id = _graph(conn, 1)
        assert AcTurnRepo.get_in_transaction(conn, turn.turn_id, 1) == turn
        assert AcTurnRepo.get_in_transaction(conn, turn.turn_id, 2) is None
        assert AcTurnRepo.get_in_transaction(conn, _id(3, 99), 1) is None
    with db.connect() as conn:
        stored = AcTurnRepo.get_in_transaction(conn, turn.turn_id, 1)
    assert stored is not None and stored.snapshot_json == turn.snapshot_json
    assert stored.snapshot_digest == "sha256:original-snapshot"
    assert stored.user_message_id == _id(5, 1) and stored.assistant_message_id == _id(6, 1)
    assert stored.created_at == 1234 and stored.schema_version == 1


def test_event_sequence_read_is_owned_and_task_scoped(db: SqlitePool) -> None:
    with db.transaction() as conn:
        first, first_run = _graph(conn, 1)
        second, second_run = _graph(conn, 2)
        other, other_run = _graph(conn, 3, owner_id=2)
        first_event = _event(conn, first, first_run, 1)
        second_event = _event(conn, second, second_run, 1)
        other_event = _event(conn, other, other_run, 1)
        assert AcEventRepo.get_by_sequence_in_transaction(conn, first.task_id, 1, 1) == first_event
        assert (
            AcEventRepo.get_by_sequence_in_transaction(conn, second.task_id, 1, 1) == second_event
        )
        assert AcEventRepo.get_by_sequence_in_transaction(conn, other.task_id, 2, 1) == other_event
        assert AcEventRepo.get_by_sequence_in_transaction(conn, first.task_id, 2, 1) is None
        assert AcEventRepo.get_by_sequence_in_transaction(conn, first.task_id, 1, 99) is None
        assert AcEventRepo.get_by_sequence_in_transaction(conn, _id(2, 99), 1, 1) is None


def test_all_acceptance_events_are_visible_in_sequence_order_without_changing_run_state(
    db: SqlitePool,
) -> None:
    with db.transaction() as conn:
        turn, run_id = _graph(conn, 1)
        events = [_event(conn, turn, run_id, sequence) for sequence in (4, 1, 3)]
        finished = _event(conn, turn, run_id, 2, event_type="run.finished")
        other, other_run = _graph(conn, 2, owner_id=2)
        _event(conn, other, other_run, 1)
        accepted = AcEventRepo.list_for_run_in_transaction(conn, run_id, 1)
        assert accepted == sorted(events, key=lambda event: event.sequence)
        assert [event.sequence for event in accepted] == [1, 3, 4]
        assert all(event.payload_json == events[0].payload_json for event in accepted)
        assert AcEventRepo.list_for_run_in_transaction(
            conn, run_id, 1, event_type="run.finished"
        ) == [finished]
        assert AcEventRepo.list_for_run_in_transaction(conn, run_id, 2) == []
        assert AcEventRepo.list_for_run_in_transaction(conn, _id(4, 99), 1) == []
        run = AcRunRepo.get_in_transaction(conn, run_id, 1)
        assert run is not None and run.state == "succeeded" and run.revision == 2


def test_owned_run_join_does_not_return_orphan_events_when_restored_parent_is_missing(
    db: SqlitePool,
) -> None:
    with db.transaction() as conn:
        turn, run_id = _graph(conn, 1)
        _event(conn, turn, run_id, 1)
    with db.connect() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("DELETE FROM ac_runs WHERE run_id = ?", (run_id,))
            assert AcEventRepo.list_for_run_in_transaction(conn, run_id, 1) == []
        finally:
            conn.execute("PRAGMA foreign_keys = ON")


def test_new_read_helpers_do_not_commit_or_rebuild_rolled_back_facts(db: SqlitePool) -> None:
    with pytest.raises(RuntimeError, match="injected"), db.transaction() as conn:
        turn, run_id = _graph(conn, 1)
        event = _event(conn, turn, run_id, 1)
        assert AcTurnRepo.get_in_transaction(conn, turn.turn_id, 1) == turn
        assert AcEventRepo.get_by_sequence_in_transaction(conn, turn.task_id, 1, 1) == event
        assert AcEventRepo.list_for_run_in_transaction(conn, run_id, 1) == [event]
        raise RuntimeError("injected")
    with db.connect() as conn:
        assert AcTurnRepo.get_in_transaction(conn, _id(3, 1), 1) is None
        assert AcEventRepo.get_by_sequence_in_transaction(conn, _id(2, 1), 1, 1) is None
        assert AcEventRepo.list_for_run_in_transaction(conn, _id(4, 1), 1) == []
        assert conn.execute("SELECT COUNT(*) FROM ac_turns").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ac_events").fetchone()[0] == 0


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _RecordingPostgresConnection:
    """A driver-shaped recorder, not PostgreSQL SQL execution or constraint validation."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, Sequence[object]]] = []

    def execute(self, sql: str, params: Sequence[object]) -> _Cursor:
        self.calls.append((sql, params))
        assert "?" not in sql and sql.count("%s") == len(params)
        return _Cursor(self.rows)


def test_postgresql_proxy_adapts_owner_scoped_read_parameters_and_maps_dict_rows(
    db: SqlitePool,
) -> None:
    with db.transaction() as conn:
        turn, run_id = _graph(conn, 1)
        event = _event(conn, turn, run_id, 1)
        turn_dict = dict(conn.execute("SELECT * FROM ac_turns").fetchone())
        event_dict = dict(conn.execute("SELECT * FROM ac_events").fetchone())
    turn_driver = _RecordingPostgresConnection([turn_dict])
    assert AcTurnRepo.get_in_transaction(_PgConnectionProxy(turn_driver), turn.turn_id, 1) == turn
    assert turn_driver.calls[0][1] == (turn.turn_id, 1)
    assert "turn_id = %s AND owner_id = %s" in turn_driver.calls[0][0]
    event_driver = _RecordingPostgresConnection([event_dict])
    proxy = _PgConnectionProxy(event_driver)
    assert AcEventRepo.get_by_sequence_in_transaction(proxy, turn.task_id, 1, 1) == event
    assert event_driver.calls[0][1] == (turn.task_id, 1, 1)
    assert "e.owner_id = %s AND e.sequence = %s" in event_driver.calls[0][0]
    assert AcEventRepo.list_for_run_in_transaction(proxy, run_id, 1) == [event]
    assert event_driver.calls[1][1] == (run_id, 1, "run.accepted")
    assert "JOIN ac_runs r" in event_driver.calls[1][0]
    assert "r.owner_id = %s" in event_driver.calls[1][0] and "LIMIT" not in event_driver.calls[1][0]


def test_postgresql_proxy_empty_owned_queries_return_no_fabricated_rows() -> None:
    proxy = _PgConnectionProxy(_RecordingPostgresConnection([]))
    assert AcTurnRepo.get_in_transaction(proxy, _id(3, 1), 1) is None
    assert AcEventRepo.get_by_sequence_in_transaction(proxy, _id(2, 1), 1, 1) is None
    assert AcEventRepo.list_for_run_in_transaction(proxy, _id(4, 1), 1) == []
