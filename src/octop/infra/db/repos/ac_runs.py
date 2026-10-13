"""SQL access to AgentComputer runs; mutations use the caller's transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

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


@dataclass(frozen=True)
class AcRunRecoveryCandidate:
    pk: int
    run_id: str
    owner_id: int

    @classmethod
    def from_row(cls, r: DbRow) -> AcRunRecoveryCandidate:
        pk, owner_id = strict_int(r["id"]), strict_int(r["owner_id"])
        run_id = r["run_id"]
        if pk < 1 or owner_id < 1 or type(run_id) is not str or str(UUID(run_id)) != run_id:
            raise ValueError("Invalid stored recovery candidate identity")
        return cls(pk=pk, run_id=run_id, owner_id=owner_id)


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
    def recovery_high_watermark(conn: Any) -> int:
        row = conn.execute(
            "SELECT MAX(id) AS through_pk FROM ac_runs "
            "WHERE state IN ('queued', 'running', 'waitingApproval', 'waitingInput') "
            "AND dispatch_phase IN ('claimed', 'dispatching', 'engineAccepted') "
            "AND reconciliation_required = 0"
        ).fetchone()
        if row is None or row["through_pk"] is None:
            return 0
        value = strict_int(row["through_pk"])
        if value < 1:
            raise ValueError("Invalid stored recovery high watermark")
        return value

    @staticmethod
    def list_recovery_candidates(
        conn: Any, box_id: str, *, after_pk: int, through_pk: int
    ) -> list[AcRunRecoveryCandidate]:
        rows = conn.execute(
            "SELECT r.id, r.run_id, r.owner_id FROM ac_runs r JOIN ac_projects p "
            "ON p.project_id = r.project_id AND p.owner_id = r.owner_id "
            "WHERE p.box_id = ? AND r.id > ? AND r.id <= ? "
            "AND r.state IN ('queued', 'running', 'waitingApproval', 'waitingInput') "
            "AND r.dispatch_phase IN ('claimed', 'dispatching', 'engineAccepted') "
            "AND r.reconciliation_required = 0 ORDER BY r.id LIMIT 100",
            (box_id, after_pk, through_pk),
        ).fetchall()
        return [AcRunRecoveryCandidate.from_row(row) for row in rows]

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
    def mark_waiting(
        conn: Any,
        run_id: str,
        owner_id: int,
        expected_revision: int,
        execution_epoch: int,
        *,
        state: str,
        updated_at: int,
    ) -> AcRunRow | None:
        if state not in ("waitingApproval", "waitingInput"):
            raise ValueError("invalid Run waiting state")
        row = conn.execute(
            "UPDATE ac_runs SET state=?,revision=revision+1,updated_at=? "
            "WHERE run_id=? AND owner_id=? AND revision=? AND execution_epoch=? "
            "AND execution_epoch>0 AND state='running' AND dispatch_phase='engineAccepted' "
            "AND reconciliation_required=0 RETURNING *",
            (state, updated_at, run_id, owner_id, expected_revision, execution_epoch),
        ).fetchone()
        return AcRunRow.from_row(row) if row is not None else None

    @staticmethod
    def mark_cancel_requested(
        conn: Any,
        run_id: str,
        owner_id: int,
        expected_revision: int,
        restore_epoch: int,
        execution_epoch: int,
        *,
        updated_at: int,
    ) -> AcRunRow | None:
        """Advance the Run revision; the separate cancel journal is the request fact."""
        row = conn.execute(
            "UPDATE ac_runs SET revision=revision+1,updated_at=? "
            "WHERE run_id=? AND owner_id=? AND revision=? AND restore_epoch=? "
            "AND execution_epoch=? AND reconciliation_required=0 "
            "AND state IN ('queued','running','waitingApproval','waitingInput') RETURNING *",
            (
                updated_at,
                run_id,
                owner_id,
                expected_revision,
                restore_epoch,
                execution_epoch,
            ),
        ).fetchone()
        return AcRunRow.from_row(row) if row is not None else None

    @staticmethod
    def mark_stopped_unstarted(
        conn: Any,
        run_id: str,
        owner_id: int,
        expected_revision: int,
        restore_epoch: int,
        *,
        updated_at: int,
    ) -> AcRunRow | None:
        """Stop only before dispatch starts, including a validated unstarted claim."""
        row = conn.execute(
            "UPDATE ac_runs SET state='stopped',revision=revision+1,updated_at=? "
            "WHERE run_id=? AND owner_id=? AND revision=? AND restore_epoch=? "
            "AND state='queued' AND reconciliation_required=0 AND ("
            "(dispatch_phase='notDispatched' AND execution_epoch=0 "
            "AND NOT EXISTS (SELECT 1 FROM ac_run_dispatches d "
            "WHERE d.run_id=ac_runs.run_id AND d.owner_id=ac_runs.owner_id)) "
            "OR (dispatch_phase='claimed' AND execution_epoch>0 "
            "AND EXISTS (SELECT 1 FROM ac_run_dispatches d "
            "WHERE d.run_id=ac_runs.run_id AND d.owner_id=ac_runs.owner_id "
            "AND d.restore_epoch=ac_runs.restore_epoch "
            "AND d.execution_epoch=ac_runs.execution_epoch AND d.revision=1 "
            "AND d.dispatch_started_at IS NULL AND d.engine_receipt_id IS NULL "
            "AND d.engine_receipt_json IS NULL AND d.engine_receipt_digest IS NULL "
            "AND d.engine_accepted_at IS NULL))) RETURNING *",
            (updated_at, run_id, owner_id, expected_revision, restore_epoch),
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
