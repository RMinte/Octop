"""SQL access to AgentComputer runs; mutations use the caller's transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, bool_int, strict_bool_int, strict_int


@dataclass(frozen=True)
class AcRunRow:
    pk: int
    run_id: str
    turn_id: str
    task_id: str
    project_id: str
    owner_id: int
    state: str
    dispatch_phase: str
    execution_epoch: int
    restore_epoch: int
    revision: int
    schema_version: int
    reconciliation_required: bool
    created_at: int
    updated_at: int

    @property
    def id(self) -> str:
        return self.run_id

    @classmethod
    def from_row(cls, r: DbRow) -> AcRunRow:
        return cls(
            pk=strict_int(r["id"]),
            run_id=str(r["run_id"]),
            turn_id=str(r["turn_id"]),
            task_id=str(r["task_id"]),
            project_id=str(r["project_id"]),
            owner_id=strict_int(r["owner_id"]),
            state=str(r["state"]),
            dispatch_phase=str(r["dispatch_phase"]),
            execution_epoch=strict_int(r["execution_epoch"]),
            restore_epoch=strict_int(r["restore_epoch"]),
            revision=strict_int(r["revision"]),
            schema_version=strict_int(r["schema_version"]),
            reconciliation_required=strict_bool_int(r["reconciliation_required"]),
            created_at=strict_int(r["created_at"]),
            updated_at=strict_int(r["updated_at"]),
        )


class AcRunRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def lock_for_update(self, conn: Any, run_id: str, owner_id: int) -> bool:
        """Serialize recovery with entry confirmation; SQLite already holds its write lock."""
        suffix = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
        return (
            conn.execute(
                "SELECT run_id FROM ac_runs WHERE run_id = ? AND owner_id = ?" + suffix,
                (run_id, owner_id),
            ).fetchone()
            is not None
        )

    @staticmethod
    def get_in_transaction(conn: Any, run_id: str, owner_id: int) -> AcRunRow | None:
        row = conn.execute(
            "SELECT * FROM ac_runs WHERE run_id = ? AND owner_id = ?", (run_id, owner_id)
        ).fetchone()
        return AcRunRow.from_row(row) if row is not None else None

    @staticmethod
    def claim(
        conn: Any,
        run_id: str,
        owner_id: int,
        expected_revision: int,
        restore_epoch: int,
        *,
        updated_at: int,
    ) -> AcRunRow | None:
        row = conn.execute(
            "UPDATE ac_runs SET dispatch_phase = 'claimed', execution_epoch = execution_epoch + 1, "
            "revision = revision + 1, updated_at = ? "
            "WHERE run_id = ? AND owner_id = ? AND revision = ? AND restore_epoch = ? "
            "AND state = 'queued' AND dispatch_phase = 'notDispatched' "
            "AND execution_epoch = 0 AND reconciliation_required = 0 "
            "AND EXISTS (SELECT 1 FROM ac_tasks t WHERE t.task_id = ac_runs.task_id "
            "AND t.project_id = ac_runs.project_id AND t.owner_id = ac_runs.owner_id "
            "AND t.preparation_state = 'ready' AND t.active_run_id = ac_runs.run_id) "
            "AND EXISTS (SELECT 1 FROM ac_projects p WHERE p.project_id = ac_runs.project_id "
            "AND p.owner_id = ac_runs.owner_id AND p.preparation_state = 'ready' "
            "AND p.active_run_id = ac_runs.run_id) RETURNING *",
            (updated_at, run_id, owner_id, expected_revision, restore_epoch),
        ).fetchone()
        return AcRunRow.from_row(row) if row is not None else None

    @staticmethod
    def mark_dispatching(
        conn: Any,
        run_id: str,
        owner_id: int,
        expected_revision: int,
        execution_epoch: int,
        *,
        updated_at: int,
    ) -> AcRunRow | None:
        row = conn.execute(
            "UPDATE ac_runs SET dispatch_phase = 'dispatching', revision = revision + 1, "
            "updated_at = ? WHERE run_id = ? AND owner_id = ? AND revision = ? "
            "AND execution_epoch = ? AND state = 'queued' AND dispatch_phase = 'claimed' "
            "AND reconciliation_required = 0 RETURNING *",
            (updated_at, run_id, owner_id, expected_revision, execution_epoch),
        ).fetchone()
        return AcRunRow.from_row(row) if row is not None else None

    @staticmethod
    def mark_engine_accepted(
        conn: Any,
        run_id: str,
        owner_id: int,
        expected_revision: int,
        execution_epoch: int,
        *,
        updated_at: int,
    ) -> AcRunRow | None:
        row = conn.execute(
            "UPDATE ac_runs SET dispatch_phase = 'engineAccepted', revision = revision + 1, "
            "updated_at = ? WHERE run_id = ? AND owner_id = ? AND revision = ? "
            "AND execution_epoch = ? AND state = 'queued' AND dispatch_phase = 'dispatching' "
            "AND reconciliation_required = 0 RETURNING *",
            (updated_at, run_id, owner_id, expected_revision, execution_epoch),
        ).fetchone()
        return AcRunRow.from_row(row) if row is not None else None

    @staticmethod
    def mark_interrupted(
        conn: Any,
        run_id: str,
        owner_id: int,
        expected_revision: int,
        execution_epoch: int,
        *,
        updated_at: int,
    ) -> AcRunRow | None:
        row = conn.execute(
            "UPDATE ac_runs SET state = 'interrupted', reconciliation_required = 1, "
            "revision = revision + 1, updated_at = ? "
            "WHERE run_id = ? AND owner_id = ? AND revision = ? AND execution_epoch = ? "
            "AND execution_epoch > 0 AND reconciliation_required = 0 "
            "AND state IN ('queued', 'running', 'waitingApproval', 'waitingInput') "
            "AND dispatch_phase IN ('claimed', 'dispatching', 'engineAccepted') RETURNING *",
            (updated_at, run_id, owner_id, expected_revision, execution_epoch),
        ).fetchone()
        return AcRunRow.from_row(row) if row is not None else None

    @staticmethod
    def insert(
        conn: Any,
        *,
        run_id: str,
        turn_id: str,
        task_id: str,
        project_id: str,
        owner_id: int,
        state: str,
        dispatch_phase: str,
        execution_epoch: int = 0,
        restore_epoch: int = 1,
        revision: int = 1,
        schema_version: int = 1,
        reconciliation_required: bool = False,
        created_at: int,
        updated_at: int,
    ) -> AcRunRow:
        row = conn.execute(
            "INSERT INTO ac_runs (run_id, turn_id, task_id, project_id, owner_id, state, dispatch_phase, execution_epoch, restore_epoch, revision, schema_version, reconciliation_required, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
            (
                run_id,
                turn_id,
                task_id,
                project_id,
                owner_id,
                state,
                dispatch_phase,
                execution_epoch,
                restore_epoch,
                revision,
                schema_version,
                bool_int(reconciliation_required),
                created_at,
                updated_at,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("AgentComputer runs insert returned no row")
        return AcRunRow.from_row(row)
