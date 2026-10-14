"""Immutable natural-completion receipts for AgentComputer Runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.repos._base import DbRow, strict_int


@dataclass(frozen=True)
class AcRunTerminalRow:
    pk: int
    receipt_id: str
    run_id: str
    turn_id: str
    task_id: str
    project_id: str
    owner_id: int
    dispatch_id: str
    engine_request_id: str
    claimant_id: str
    restore_epoch: int
    execution_epoch: int
    receipt_json: str
    receipt_digest: str
    schema_version: int
    completed_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> AcRunTerminalRow:
        return cls(
            pk=strict_int(row["id"]),
            receipt_id=str(row["receipt_id"]),
            run_id=str(row["run_id"]),
            turn_id=str(row["turn_id"]),
            task_id=str(row["task_id"]),
            project_id=str(row["project_id"]),
            owner_id=strict_int(row["owner_id"]),
            dispatch_id=str(row["dispatch_id"]),
            engine_request_id=str(row["engine_request_id"]),
            claimant_id=str(row["claimant_id"]),
            restore_epoch=strict_int(row["restore_epoch"]),
            execution_epoch=strict_int(row["execution_epoch"]),
            receipt_json=str(row["receipt_json"]),
            receipt_digest=str(row["receipt_digest"]),
            schema_version=strict_int(row["schema_version"]),
            completed_at=strict_int(row["completed_at"]),
        )


class AcRunTerminalRepo:
    @staticmethod
    def get_in_transaction(conn: Any, run_id: str, owner_id: int) -> AcRunTerminalRow | None:
        row = conn.execute(
            "SELECT * FROM ac_run_terminals WHERE run_id=? AND owner_id=?", (run_id, owner_id)
        ).fetchone()
        return AcRunTerminalRow.from_row(row) if row is not None else None

    @staticmethod
    def insert(
        conn: Any,
        *,
        receipt_id: str,
        run_id: str,
        turn_id: str,
        task_id: str,
        project_id: str,
        owner_id: int,
        dispatch_id: str,
        engine_request_id: str,
        claimant_id: str,
        restore_epoch: int,
        execution_epoch: int,
        receipt_json: str,
        receipt_digest: str,
        completed_at: int,
    ) -> AcRunTerminalRow:
        row = conn.execute(
            "INSERT INTO ac_run_terminals (receipt_id,run_id,turn_id,task_id,project_id,"
            "owner_id,dispatch_id,engine_request_id,claimant_id,restore_epoch,execution_epoch,"
            "receipt_json,receipt_digest,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *",
            (
                receipt_id,
                run_id,
                turn_id,
                task_id,
                project_id,
                owner_id,
                dispatch_id,
                engine_request_id,
                claimant_id,
                restore_epoch,
                execution_epoch,
                receipt_json,
                receipt_digest,
                completed_at,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("AgentComputer terminal receipt insert returned no row")
        return AcRunTerminalRow.from_row(row)
