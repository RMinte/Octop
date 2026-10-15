"""SQL access to durable AgentComputer Run cancellation requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int
from octop.infra.db.repos.ac_commands import AcCommandRow


@dataclass(frozen=True)
class AcRunCancellationRow:
    pk: int
    run_id: str
    turn_id: str
    task_id: str
    project_id: str | None
    owner_id: int
    command_id: str
    restore_epoch: int
    execution_epoch: int
    run_revision_at_request: int
    phase: str
    revision: int
    requested_at: int
    stopped_at: int | None

    @classmethod
    def from_row(cls, row: DbRow) -> AcRunCancellationRow:
        return cls(
            pk=strict_int(row["id"]),
            run_id=str(row["run_id"]),
            turn_id=str(row["turn_id"]),
            task_id=str(row["task_id"]),
            project_id=str(row["project_id"]) if row["project_id"] is not None else None,
            owner_id=strict_int(row["owner_id"]),
            command_id=str(row["command_id"]),
            restore_epoch=strict_int(row["restore_epoch"]),
            execution_epoch=strict_int(row["execution_epoch"]),
            run_revision_at_request=strict_int(row["run_revision_at_request"]),
            phase=str(row["phase"]),
            revision=strict_int(row["revision"]),
            requested_at=strict_int(row["requested_at"]),
            stopped_at=strict_int(row["stopped_at"]) if row["stopped_at"] is not None else None,
        )


class AcRunCancellationRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def get_in_transaction(conn: Any, run_id: str, owner_id: int) -> AcRunCancellationRow | None:
        row = conn.execute(
            "SELECT * FROM ac_run_cancellations WHERE run_id = ? AND owner_id = ?",
            (run_id, owner_id),
        ).fetchone()
        return AcRunCancellationRow.from_row(row) if row is not None else None

    @staticmethod
    def list_commands_in_transaction(conn: Any, run_id: str, owner_id: int) -> list[AcCommandRow]:
        rows = conn.execute(
            "SELECT * FROM ac_commands WHERE operation = 'run.cancel' "
            "AND target_id = ? AND owner_id = ? ORDER BY id",
            (run_id, owner_id),
        ).fetchall()
        return [AcCommandRow.from_row(row) for row in rows]

    @staticmethod
    def insert(
        conn: Any,
        *,
        run_id: str,
        turn_id: str,
        task_id: str,
        project_id: str | None,
        owner_id: int,
        command_id: str,
        restore_epoch: int,
        execution_epoch: int,
        run_revision_at_request: int,
        phase: str,
        requested_at: int,
        stopped_at: int | None,
    ) -> AcRunCancellationRow | None:
        row = conn.execute(
            "INSERT INTO ac_run_cancellations "
            "(run_id, turn_id, task_id, project_id, owner_id, command_id, "
            "restore_epoch, execution_epoch, run_revision_at_request, phase, "
            "revision, requested_at, stopped_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT DO NOTHING RETURNING *",
            (
                run_id,
                turn_id,
                task_id,
                project_id,
                owner_id,
                command_id,
                restore_epoch,
                execution_epoch,
                run_revision_at_request,
                phase,
                requested_at,
                stopped_at,
            ),
        ).fetchone()
        return AcRunCancellationRow.from_row(row) if row is not None else None
