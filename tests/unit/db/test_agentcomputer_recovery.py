"""SQL candidate scans only; these rows are not managed execution fixtures."""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit.db.test_agentcomputer_dispatch import _graph, _id
from tests.unit.db.test_agentcomputer_dispatch import db as db

from octop.infra.db.pool import SqlitePool, _PgConnectionProxy
from octop.infra.db.repos.ac_runs import AcRunRecoveryCandidate, AcRunRepo


def _progress(
    conn: Any, index: int, *, owner_id: int = 1, box_id: str | None = None
) -> dict[str, Any]:
    graph = _graph(conn, index, owner_id=owner_id)
    conn.execute(
        "UPDATE ac_runs SET dispatch_phase='claimed',execution_epoch=1 WHERE run_id=?",
        (graph["run_id"],),
    )
    if box_id is not None:
        conn.execute(
            "UPDATE ac_projects SET box_id=? WHERE project_id=?", (box_id, graph["project_id"])
        )
    return graph


def test_recovery_high_watermark_excludes_unclaimed_terminal_and_reconciled_rows(
    db: SqlitePool,
) -> None:
    with db.transaction() as conn:
        assert AcRunRepo.recovery_high_watermark(conn) == 0
        graph = _graph(conn, 1)
        assert AcRunRepo.recovery_high_watermark(conn) == 0
        active = _progress(conn, 2)
        terminal = _progress(conn, 3)
        conn.execute("UPDATE ac_runs SET state='succeeded' WHERE run_id=?", (terminal["run_id"],))
        reconciled = _progress(conn, 4)
        conn.execute(
            "UPDATE ac_runs SET state='interrupted',reconciliation_required=1 WHERE run_id=?",
            (reconciled["run_id"],),
        )
        expected = conn.execute(
            "SELECT id FROM ac_runs WHERE run_id=?", (active["run_id"],)
        ).fetchone()[0]
        assert AcRunRepo.recovery_high_watermark(conn) == expected
        rows = AcRunRepo.list_recovery_candidates(conn, _id(9), after_pk=0, through_pk=100)
        assert [row.run_id for row in rows] == [active["run_id"]]
        assert graph["run_id"] not in {row.run_id for row in rows}


def test_local_box_join_keeps_owner_binding_and_includes_all_local_owners(db: SqlitePool) -> None:
    with db.transaction() as conn:
        one = _progress(conn, 1)
        two = _progress(conn, 2, owner_id=2)
        foreign = _progress(conn, 3, owner_id=2, box_id=_id(9, 2))
        through = AcRunRepo.recovery_high_watermark(conn)
        rows = AcRunRepo.list_recovery_candidates(conn, _id(9), after_pk=0, through_pk=through)
        assert [(row.run_id, row.owner_id) for row in rows] == [
            (one["run_id"], 1),
            (two["run_id"], 2),
        ]
        assert foreign["run_id"] not in {row.run_id for row in rows}
        assert (
            AcRunRepo.list_recovery_candidates(conn, _id(9, 99), after_pk=0, through_pk=through)
            == []
        )


def test_keyset_pagination_is_fixed_at_100_and_excludes_post_watermark_runs(db: SqlitePool) -> None:
    with db.transaction() as conn:
        expected = [_progress(conn, index)["run_id"] for index in range(1, 106)]
        through = AcRunRepo.recovery_high_watermark(conn)
        first = AcRunRepo.list_recovery_candidates(conn, _id(9), after_pk=0, through_pk=through)
        assert len(first) == 100
        later = _progress(conn, 106)
        second = AcRunRepo.list_recovery_candidates(
            conn, _id(9), after_pk=first[-1].pk, through_pk=through
        )
        assert len(second) == 5
        assert [row.run_id for row in first + second] == expected
        assert later["run_id"] not in {row.run_id for row in first + second}
        assert (
            AcRunRepo.list_recovery_candidates(
                conn, _id(9), after_pk=second[-1].pk, through_pk=through
            )
            == []
        )
        assert AcRunRepo.recovery_high_watermark(conn) > through


@pytest.mark.parametrize(
    "change",
    [
        {"id": 1.5},
        {"id": True},
        {"id": 0},
        {"owner_id": "1"},
        {"owner_id": 1.5},
        {"owner_id": True},
        {"owner_id": 0},
        {"run_id": 1},
        {"run_id": "bad"},
        {"run_id": "AAAAAAAA-0000-4000-8000-000000000001"},
    ],
)
def test_candidate_decoder_rejects_untrusted_identity_fields(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        AcRunRecoveryCandidate.from_row({"id": 1, "run_id": _id(4), "owner_id": 1, **change})


def test_candidate_decoder_preserves_legitimate_integer_and_uuid_fields() -> None:
    assert AcRunRecoveryCandidate.from_row(
        {"id": 5, "run_id": _id(4), "owner_id": 2}
    ) == AcRunRecoveryCandidate(pk=5, run_id=_id(4), owner_id=2)


class _PgCursor:
    def fetchone(self) -> dict[str, int]:
        return {"through_pk": 4}

    def fetchall(self) -> list[dict[str, Any]]:
        return [{"id": 4, "run_id": _id(4), "owner_id": 1}]


class _PgDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: Any = None) -> _PgCursor:
        values = tuple(params or ())
        assert "?" not in sql and sql.count("%s") == len(values)
        self.calls.append((sql, values))
        return _PgCursor()


def test_postgresql_scans_keep_scope_keyset_and_placeholders_without_real_database() -> None:
    driver = _PgDriver()
    conn = _PgConnectionProxy(driver)
    assert AcRunRepo.recovery_high_watermark(conn) == 4
    assert AcRunRepo.list_recovery_candidates(conn, _id(9), after_pk=1, through_pk=4) == [
        AcRunRecoveryCandidate(pk=4, run_id=_id(4), owner_id=1)
    ]
    sql, parameters = driver.calls[1]
    assert "p.owner_id = r.owner_id" in sql and "p.project_id = r.project_id" in sql
    assert "WHERE r.id > %s AND r.id <= %s" in sql
    assert "t.task_id=r.task_id AND t.owner_id=r.owner_id" in sql
    assert "t.project_id=r.project_id AND p.box_id=%s" in sql
    assert "t.project_id IS NULL AND t.standalone_box_id=%s" in sql
    assert "ORDER BY r.id LIMIT 100" in sql
    assert parameters == (1, 4, _id(9), _id(9))
