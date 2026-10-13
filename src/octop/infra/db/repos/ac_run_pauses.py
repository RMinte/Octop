"""SQL-only durable Run pauses and their immutable adjudication facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, strict_int


@dataclass(frozen=True)
class AcRunPauseRow:
    pk: int
    pause_id: str
    kind: str
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
    phase: str
    intent_json: str
    intent_digest: str
    schema_version: int
    revision: int
    created_at: int
    updated_at: int
    expires_at: int
    decision_command_id: str | None
    decision_idempotency_key: str | None
    decision_request_json: str | None
    decision_request_digest: str | None
    decision_receipt_json: str | None
    decision_receipt_digest: str | None
    decided_at: int | None

    @classmethod
    def from_row(cls, r: DbRow) -> AcRunPauseRow:
        def optional_text(key: str) -> str | None:
            return str(r[key]) if r[key] is not None else None

        return cls(
            pk=strict_int(r["id"]),
            pause_id=str(r["pause_id"]),
            kind=str(r["kind"]),
            run_id=str(r["run_id"]),
            turn_id=str(r["turn_id"]),
            task_id=str(r["task_id"]),
            project_id=str(r["project_id"]),
            owner_id=strict_int(r["owner_id"]),
            dispatch_id=str(r["dispatch_id"]),
            engine_request_id=str(r["engine_request_id"]),
            claimant_id=str(r["claimant_id"]),
            restore_epoch=strict_int(r["restore_epoch"]),
            execution_epoch=strict_int(r["execution_epoch"]),
            phase=str(r["phase"]),
            intent_json=str(r["intent_json"]),
            intent_digest=str(r["intent_digest"]),
            schema_version=strict_int(r["schema_version"]),
            revision=strict_int(r["revision"]),
            created_at=strict_int(r["created_at"]),
            updated_at=strict_int(r["updated_at"]),
            expires_at=strict_int(r["expires_at"]),
            decision_command_id=optional_text("decision_command_id"),
            decision_idempotency_key=optional_text("decision_idempotency_key"),
            decision_request_json=optional_text("decision_request_json"),
            decision_request_digest=optional_text("decision_request_digest"),
            decision_receipt_json=optional_text("decision_receipt_json"),
            decision_receipt_digest=optional_text("decision_receipt_digest"),
            decided_at=(strict_int(r["decided_at"]) if r["decided_at"] is not None else None),
        )


@dataclass(frozen=True)
class AcRunPauseRecoveryCandidate:
    pk: int
    pause_id: str
    owner_id: int

    @classmethod
    def from_row(cls, row: DbRow) -> AcRunPauseRecoveryCandidate:
        pk, owner_id, pause_id = strict_int(row["id"]), strict_int(row["owner_id"]), row["pause_id"]
        if pk < 1 or owner_id < 1 or type(pause_id) is not str or str(UUID(pause_id)) != pause_id:
            raise ValueError("Invalid stored pause recovery identity")
        return cls(pk=pk, pause_id=pause_id, owner_id=owner_id)


class AcRunPauseRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def get_in_transaction(conn: Any, pause_id: str, owner_id: int) -> AcRunPauseRow | None:
        row = conn.execute(
            "SELECT * FROM ac_run_pauses WHERE pause_id = ? AND owner_id = ?",
            (pause_id, owner_id),
        ).fetchone()
        return AcRunPauseRow.from_row(row) if row is not None else None

    @staticmethod
    def get_by_run_in_transaction(conn: Any, run_id: str, owner_id: int) -> AcRunPauseRow | None:
        row = conn.execute(
            "SELECT * FROM ac_run_pauses WHERE run_id = ? AND owner_id = ? "
            "AND phase IN ('pending', 'decided')",
            (run_id, owner_id),
        ).fetchone()
        return AcRunPauseRow.from_row(row) if row is not None else None

    @staticmethod
    def list_for_run_in_transaction(conn: Any, run_id: str, owner_id: int) -> list[AcRunPauseRow]:
        rows = conn.execute(
            "SELECT * FROM ac_run_pauses WHERE run_id = ? AND owner_id = ? ORDER BY id",
            (run_id, owner_id),
        ).fetchall()
        return map_rows(rows, AcRunPauseRow)

    @staticmethod
    def get_by_command_in_transaction(
        conn: Any, command_id: str, owner_id: int
    ) -> AcRunPauseRow | None:
        row = conn.execute(
            "SELECT * FROM ac_run_pauses WHERE decision_command_id = ? AND owner_id = ?",
            (command_id, owner_id),
        ).fetchone()
        return AcRunPauseRow.from_row(row) if row is not None else None

    @staticmethod
    def get_by_key_in_transaction(
        conn: Any, owner_id: int, idempotency_key: str
    ) -> AcRunPauseRow | None:
        row = conn.execute(
            "SELECT * FROM ac_run_pauses WHERE owner_id = ? AND decision_idempotency_key = ?",
            (owner_id, idempotency_key),
        ).fetchone()
        return AcRunPauseRow.from_row(row) if row is not None else None

    @staticmethod
    def command_exists_in_transaction(conn: Any, command_id: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM ac_run_pauses WHERE decision_command_id = ?", (command_id,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def insert(
        conn: Any,
        *,
        pause_id: str,
        kind: str,
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
        intent_json: str,
        intent_digest: str,
        created_at: int,
        expires_at: int,
    ) -> AcRunPauseRow | None:
        row = conn.execute(
            "INSERT INTO ac_run_pauses (pause_id,kind,run_id,turn_id,task_id,project_id,"
            "owner_id,dispatch_id,engine_request_id,claimant_id,restore_epoch,execution_epoch,"
            "intent_json,intent_digest,created_at,updated_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING *",
            (
                pause_id,
                kind,
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
                intent_json,
                intent_digest,
                created_at,
                created_at,
                expires_at,
            ),
        ).fetchone()
        return AcRunPauseRow.from_row(row) if row is not None else None

    @staticmethod
    def record_decision(
        conn: Any,
        *,
        pause_id: str,
        owner_id: int,
        expected_revision: int,
        command_id: str,
        idempotency_key: str,
        request_json: str,
        request_digest: str,
        receipt_json: str,
        receipt_digest: str,
        decided_at: int,
    ) -> AcRunPauseRow | None:
        row = conn.execute(
            "UPDATE ac_run_pauses SET phase='decided', revision=revision+1, "
            "decision_command_id=?, decision_idempotency_key=?, decision_request_json=?, "
            "decision_request_digest=?, decision_receipt_json=?, decision_receipt_digest=?, "
            "decided_at=?, updated_at=? WHERE pause_id=? AND owner_id=? AND revision=? "
            "AND phase='pending' AND decision_command_id IS NULL RETURNING *",
            (
                command_id,
                idempotency_key,
                request_json,
                request_digest,
                receipt_json,
                receipt_digest,
                decided_at,
                decided_at,
                pause_id,
                owner_id,
                expected_revision,
            ),
        ).fetchone()
        return AcRunPauseRow.from_row(row) if row is not None else None

    @staticmethod
    def mark_expired(
        conn: Any, pause_id: str, owner_id: int, expected_revision: int, *, updated_at: int
    ) -> AcRunPauseRow | None:
        row = conn.execute(
            "UPDATE ac_run_pauses SET phase='expired',revision=revision+1,updated_at=? "
            "WHERE pause_id=? AND owner_id=? AND revision=? AND phase IN ('pending','decided') "
            "RETURNING *",
            (updated_at, pause_id, owner_id, expected_revision),
        ).fetchone()
        return AcRunPauseRow.from_row(row) if row is not None else None

    @staticmethod
    def recovery_high_watermark(conn: Any) -> int:
        row = conn.execute(
            "SELECT MAX(id) AS through_pk FROM ac_run_pauses WHERE phase IN ('pending','decided')"
        ).fetchone()
        if row is None or row["through_pk"] is None:
            return 0
        value = strict_int(row["through_pk"])
        if value < 1:
            raise ValueError("Invalid stored pause recovery high watermark")
        return value

    @staticmethod
    def list_recovery_candidates(
        conn: Any, box_id: str, *, after_pk: int, through_pk: int
    ) -> list[AcRunPauseRecoveryCandidate]:
        rows = conn.execute(
            "SELECT q.id,q.pause_id,q.owner_id FROM ac_run_pauses q "
            "JOIN ac_projects p ON p.project_id=q.project_id AND p.owner_id=q.owner_id "
            "WHERE p.box_id=? AND q.id>? AND q.id<=? AND q.phase IN ('pending','decided') "
            "ORDER BY q.id LIMIT 100",
            (box_id, after_pk, through_pk),
        ).fetchall()
        return [AcRunPauseRecoveryCandidate.from_row(row) for row in rows]
