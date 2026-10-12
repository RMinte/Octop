"""SQL access to AgentComputer runs; mutations use the caller's transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, bool_int


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
            pk=int(r["id"]),
            run_id=str(r["run_id"]),
            turn_id=str(r["turn_id"]),
            task_id=str(r["task_id"]),
            project_id=str(r["project_id"]),
            owner_id=int(r["owner_id"]),
            state=str(r["state"]),
            dispatch_phase=str(r["dispatch_phase"]),
            execution_epoch=int(r["execution_epoch"]),
            restore_epoch=int(r["restore_epoch"]),
            revision=int(r["revision"]),
            schema_version=int(r["schema_version"]),
            reconciliation_required=bool(r["reconciliation_required"]),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


class AcRunRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def get_in_transaction(conn: Any, run_id: str, owner_id: int) -> AcRunRow | None:
        row = conn.execute(
            "SELECT * FROM ac_runs WHERE run_id = ? AND owner_id = ?", (run_id, owner_id)
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
