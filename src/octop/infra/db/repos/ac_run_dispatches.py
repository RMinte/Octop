"""Fixed Run dispatch facts; every mutation uses the caller's transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int


@dataclass(frozen=True)
class AcRunDispatchRow:
    pk: int
    dispatch_id: str
    run_id: str
    turn_id: str
    task_id: str
    project_id: str | None
    owner_id: int
    execution_epoch: int
    restore_epoch: int
    claimant_id: str
    engine_request_id: str
    intent_json: str
    intent_digest: str
    schema_version: int
    revision: int
    created_at: int
    updated_at: int
    dispatch_started_at: int | None
    engine_receipt_id: str | None
    engine_receipt_json: str | None
    engine_receipt_digest: str | None
    engine_accepted_at: int | None

    @property
    def id(self) -> str:
        return self.dispatch_id

    @classmethod
    def from_row(cls, r: DbRow) -> AcRunDispatchRow:
        return cls(
            pk=strict_int(r["id"]),
            dispatch_id=str(r["dispatch_id"]),
            run_id=str(r["run_id"]),
            turn_id=str(r["turn_id"]),
            task_id=str(r["task_id"]),
            project_id=str(r["project_id"]) if r["project_id"] is not None else None,
            owner_id=strict_int(r["owner_id"]),
            execution_epoch=strict_int(r["execution_epoch"]),
            restore_epoch=strict_int(r["restore_epoch"]),
            claimant_id=str(r["claimant_id"]),
            engine_request_id=str(r["engine_request_id"]),
            intent_json=str(r["intent_json"]),
            intent_digest=str(r["intent_digest"]),
            schema_version=strict_int(r["schema_version"]),
            revision=strict_int(r["revision"]),
            created_at=strict_int(r["created_at"]),
            updated_at=strict_int(r["updated_at"]),
            dispatch_started_at=(
                strict_int(r["dispatch_started_at"])
                if r["dispatch_started_at"] is not None
                else None
            ),
            engine_receipt_id=(
                str(r["engine_receipt_id"]) if r["engine_receipt_id"] is not None else None
            ),
            engine_receipt_json=(
                str(r["engine_receipt_json"]) if r["engine_receipt_json"] is not None else None
            ),
            engine_receipt_digest=(
                str(r["engine_receipt_digest"]) if r["engine_receipt_digest"] is not None else None
            ),
            engine_accepted_at=(
                strict_int(r["engine_accepted_at"]) if r["engine_accepted_at"] is not None else None
            ),
        )


class AcRunDispatchRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get(self, run_id: str, owner_id: int) -> AcRunDispatchRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, run_id, owner_id)

    @staticmethod
    def get_in_transaction(conn: Any, run_id: str, owner_id: int) -> AcRunDispatchRow | None:
        row = conn.execute(
            "SELECT * FROM ac_run_dispatches WHERE run_id = ? AND owner_id = ?",
            (run_id, owner_id),
        ).fetchone()
        return AcRunDispatchRow.from_row(row) if row is not None else None

    @staticmethod
    def insert(
        conn: Any,
        *,
        dispatch_id: str,
        run_id: str,
        turn_id: str,
        task_id: str,
        project_id: str | None,
        owner_id: int,
        execution_epoch: int,
        restore_epoch: int,
        claimant_id: str,
        engine_request_id: str,
        intent_json: str,
        intent_digest: str,
        created_at: int,
        updated_at: int,
    ) -> AcRunDispatchRow:
        row = conn.execute(
            "INSERT INTO ac_run_dispatches (dispatch_id, run_id, turn_id, task_id, project_id, "
            "owner_id, execution_epoch, restore_epoch, claimant_id, engine_request_id, "
            "intent_json, intent_digest, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
            (
                dispatch_id,
                run_id,
                turn_id,
                task_id,
                project_id,
                owner_id,
                execution_epoch,
                restore_epoch,
                claimant_id,
                engine_request_id,
                intent_json,
                intent_digest,
                created_at,
                updated_at,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("AgentComputer dispatch insert returned no row")
        return AcRunDispatchRow.from_row(row)

    @staticmethod
    def start_dispatch(
        conn: Any, run_id: str, owner_id: int, expected_revision: int, *, updated_at: int
    ) -> bool:
        cursor = conn.execute(
            "UPDATE ac_run_dispatches SET dispatch_started_at = ?, revision = revision + 1, "
            "updated_at = ? WHERE run_id = ? AND owner_id = ? AND revision = ? "
            "AND dispatch_started_at IS NULL AND engine_receipt_id IS NULL "
            "AND engine_receipt_json IS NULL AND engine_receipt_digest IS NULL "
            "AND engine_accepted_at IS NULL",
            (updated_at, updated_at, run_id, owner_id, expected_revision),
        )
        return bool(cursor.rowcount == 1)

    @staticmethod
    def record_acceptance(
        conn: Any,
        run_id: str,
        owner_id: int,
        expected_revision: int,
        *,
        engine_receipt_id: str,
        engine_receipt_json: str,
        engine_receipt_digest: str,
        engine_accepted_at: int,
        updated_at: int,
    ) -> bool:
        cursor = conn.execute(
            "UPDATE ac_run_dispatches SET engine_receipt_id = ?, engine_receipt_json = ?, "
            "engine_receipt_digest = ?, engine_accepted_at = ?, revision = revision + 1, "
            "updated_at = ? WHERE run_id = ? AND owner_id = ? AND revision = ? "
            "AND dispatch_started_at IS NOT NULL AND engine_receipt_id IS NULL "
            "AND engine_receipt_json IS NULL AND engine_receipt_digest IS NULL "
            "AND engine_accepted_at IS NULL",
            (
                engine_receipt_id,
                engine_receipt_json,
                engine_receipt_digest,
                engine_accepted_at,
                updated_at,
                run_id,
                owner_id,
                expected_revision,
            ),
        )
        return bool(cursor.rowcount == 1)
