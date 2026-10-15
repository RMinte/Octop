"""Fixed tool-effect facts; mutations use the caller's transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, strict_int


@dataclass(frozen=True)
class AcToolEffectRow:
    pk: int
    effect_id: str
    run_id: str
    turn_id: str
    task_id: str
    project_id: str | None
    owner_id: int
    dispatch_id: str
    engine_request_id: str
    claimant_id: str
    restore_epoch: int
    execution_epoch: int
    tool_call_id: str
    execution_attempt: int
    phase: str
    intent_json: str
    intent_digest: str
    schema_version: int
    revision: int
    created_at: int
    updated_at: int
    dispatch_started_at: int | None
    terminal_json: str | None
    terminal_digest: str | None
    terminal_at: int | None

    @property
    def id(self) -> str:
        return self.effect_id

    @classmethod
    def from_row(cls, r: DbRow) -> AcToolEffectRow:
        return cls(
            pk=strict_int(r["id"]),
            effect_id=str(r["effect_id"]),
            run_id=str(r["run_id"]),
            turn_id=str(r["turn_id"]),
            task_id=str(r["task_id"]),
            project_id=str(r["project_id"]) if r["project_id"] is not None else None,
            owner_id=strict_int(r["owner_id"]),
            dispatch_id=str(r["dispatch_id"]),
            engine_request_id=str(r["engine_request_id"]),
            claimant_id=str(r["claimant_id"]),
            restore_epoch=strict_int(r["restore_epoch"]),
            execution_epoch=strict_int(r["execution_epoch"]),
            tool_call_id=str(r["tool_call_id"]),
            execution_attempt=strict_int(r["execution_attempt"]),
            phase=str(r["phase"]),
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
            terminal_json=(str(r["terminal_json"]) if r["terminal_json"] is not None else None),
            terminal_digest=(
                str(r["terminal_digest"]) if r["terminal_digest"] is not None else None
            ),
            terminal_at=(strict_int(r["terminal_at"]) if r["terminal_at"] is not None else None),
        )


@dataclass(frozen=True)
class AcToolEffectRecoveryCandidate:
    pk: int
    effect_id: str
    owner_id: int

    @classmethod
    def from_row(cls, r: DbRow) -> AcToolEffectRecoveryCandidate:
        pk, owner_id = strict_int(r["id"]), strict_int(r["owner_id"])
        effect_id = r["effect_id"]
        if (
            pk < 1
            or owner_id < 1
            or type(effect_id) is not str
            or str(UUID(effect_id)) != effect_id
        ):
            raise ValueError("Invalid stored effect recovery candidate identity")
        return cls(pk=pk, effect_id=effect_id, owner_id=owner_id)


class AcToolEffectRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get(self, effect_id: str, owner_id: int) -> AcToolEffectRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, effect_id, owner_id)

    @staticmethod
    def get_in_transaction(conn: Any, effect_id: str, owner_id: int) -> AcToolEffectRow | None:
        row = conn.execute(
            "SELECT * FROM ac_tool_effects WHERE effect_id = ? AND owner_id = ?",
            (effect_id, owner_id),
        ).fetchone()
        return AcToolEffectRow.from_row(row) if row is not None else None

    @staticmethod
    def get_by_call_in_transaction(
        conn: Any, run_id: str, owner_id: int, tool_call_id: str, execution_attempt: int
    ) -> AcToolEffectRow | None:
        row = conn.execute(
            "SELECT * FROM ac_tool_effects WHERE run_id = ? AND owner_id = ? "
            "AND tool_call_id = ? AND execution_attempt = ?",
            (run_id, owner_id, tool_call_id, execution_attempt),
        ).fetchone()
        return AcToolEffectRow.from_row(row) if row is not None else None

    @staticmethod
    def list_for_run_in_transaction(conn: Any, run_id: str, owner_id: int) -> list[AcToolEffectRow]:
        rows = conn.execute(
            "SELECT * FROM ac_tool_effects WHERE run_id = ? AND owner_id = ? ORDER BY id",
            (run_id, owner_id),
        ).fetchall()
        return map_rows(rows, AcToolEffectRow)

    @staticmethod
    def get_pending_in_transaction(conn: Any, run_id: str, owner_id: int) -> AcToolEffectRow | None:
        row = conn.execute(
            "SELECT * FROM ac_tool_effects WHERE run_id = ? AND owner_id = ? "
            "AND phase IN ('prepared', 'dispatching', 'outcomeUnknown')",
            (run_id, owner_id),
        ).fetchone()
        return AcToolEffectRow.from_row(row) if row is not None else None

    @staticmethod
    def recovery_high_watermark(conn: Any) -> int:
        row = conn.execute(
            "SELECT MAX(id) AS through_pk FROM ac_tool_effects "
            "WHERE phase IN ('prepared', 'dispatching')"
        ).fetchone()
        if row is None or row["through_pk"] is None:
            return 0
        value = strict_int(row["through_pk"])
        if value < 1:
            raise ValueError("Invalid stored effect recovery high watermark")
        return value

    @staticmethod
    def list_recovery_candidates(
        conn: Any, box_id: str, *, after_pk: int, through_pk: int
    ) -> list[AcToolEffectRecoveryCandidate]:
        rows = conn.execute(
            "SELECT e.id, e.effect_id, e.owner_id FROM ac_tool_effects e "
            "JOIN ac_runs r ON r.run_id = e.run_id AND r.owner_id = e.owner_id "
            "AND r.turn_id = e.turn_id AND r.task_id = e.task_id AND r.project_id = e.project_id "
            "JOIN ac_projects p ON p.project_id = r.project_id AND p.owner_id = r.owner_id "
            "WHERE p.box_id = ? AND e.id > ? AND e.id <= ? "
            "AND e.phase IN ('prepared', 'dispatching') ORDER BY e.id LIMIT 100",
            (box_id, after_pk, through_pk),
        ).fetchall()
        return [AcToolEffectRecoveryCandidate.from_row(row) for row in rows]

    @staticmethod
    def insert(
        conn: Any,
        *,
        effect_id: str,
        run_id: str,
        turn_id: str,
        task_id: str,
        project_id: str | None,
        owner_id: int,
        dispatch_id: str,
        engine_request_id: str,
        claimant_id: str,
        restore_epoch: int,
        execution_epoch: int,
        tool_call_id: str,
        execution_attempt: int,
        intent_json: str,
        intent_digest: str,
        created_at: int,
        updated_at: int,
    ) -> AcToolEffectRow | None:
        row = conn.execute(
            "INSERT INTO ac_tool_effects (effect_id, run_id, turn_id, task_id, project_id, "
            "owner_id, dispatch_id, engine_request_id, claimant_id, restore_epoch, "
            "execution_epoch, tool_call_id, execution_attempt, intent_json, intent_digest, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING RETURNING *",
            (
                effect_id,
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
                tool_call_id,
                execution_attempt,
                intent_json,
                intent_digest,
                created_at,
                updated_at,
            ),
        ).fetchone()
        return AcToolEffectRow.from_row(row) if row is not None else None

    @staticmethod
    def mark_dispatching(
        conn: Any, effect_id: str, owner_id: int, expected_revision: int, *, updated_at: int
    ) -> AcToolEffectRow | None:
        row = conn.execute(
            "UPDATE ac_tool_effects SET phase = 'dispatching', dispatch_started_at = ?, "
            "revision = revision + 1, updated_at = ? "
            "WHERE effect_id = ? AND owner_id = ? AND revision = ? AND phase = 'prepared' "
            "AND dispatch_started_at IS NULL AND terminal_json IS NULL "
            "AND terminal_digest IS NULL AND terminal_at IS NULL RETURNING *",
            (updated_at, updated_at, effect_id, owner_id, expected_revision),
        ).fetchone()
        return AcToolEffectRow.from_row(row) if row is not None else None

    @staticmethod
    def record_terminal(
        conn: Any,
        effect_id: str,
        owner_id: int,
        expected_revision: int,
        *,
        expected_phase: Literal["prepared", "dispatching"],
        terminal_json: str,
        terminal_digest: str,
        terminal_at: int,
        updated_at: int,
    ) -> AcToolEffectRow | None:
        row = conn.execute(
            "UPDATE ac_tool_effects SET phase = 'terminal', terminal_json = ?, "
            "terminal_digest = ?, terminal_at = ?, revision = revision + 1, updated_at = ? "
            "WHERE effect_id = ? AND owner_id = ? AND revision = ? AND phase = ? "
            "AND phase IN ('prepared', 'dispatching') AND terminal_json IS NULL "
            "AND terminal_digest IS NULL AND terminal_at IS NULL RETURNING *",
            (
                terminal_json,
                terminal_digest,
                terminal_at,
                updated_at,
                effect_id,
                owner_id,
                expected_revision,
                expected_phase,
            ),
        ).fetchone()
        return AcToolEffectRow.from_row(row) if row is not None else None

    @staticmethod
    def mark_outcome_unknown(
        conn: Any, effect_id: str, owner_id: int, expected_revision: int, *, updated_at: int
    ) -> AcToolEffectRow | None:
        row = conn.execute(
            "UPDATE ac_tool_effects SET phase = 'outcomeUnknown', revision = revision + 1, "
            "updated_at = ? WHERE effect_id = ? AND owner_id = ? AND revision = ? "
            "AND phase IN ('prepared', 'dispatching') AND terminal_json IS NULL "
            "AND terminal_digest IS NULL AND terminal_at IS NULL RETURNING *",
            (updated_at, effect_id, owner_id, expected_revision),
        ).fetchone()
        return AcToolEffectRow.from_row(row) if row is not None else None
