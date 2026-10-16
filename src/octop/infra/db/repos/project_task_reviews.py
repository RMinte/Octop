"""SQL-only, one-attempt approval reviews and their independently billed usage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int
from octop.infra.db.repos.project_tasks import ProjectTaskRepo

_LEASE_MS = 90_000


@dataclass(frozen=True)
class TaskReviewRow:
    review_id: str
    task_id: str
    turn_id: str
    owner_id: int
    execution_host: str
    target_id: str
    action_digest: str
    proposal_json: str
    model_ref: str
    created_at: int
    updated_at: int
    expires_at: int
    status: str = "queued"
    decision: str = ""
    reason_code: str = ""
    worker_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    pk: int = 0

    @classmethod
    def from_row(cls, row: DbRow) -> TaskReviewRow:
        return cls(
            review_id=str(row["review_id"]),
            task_id=str(row["task_id"]),
            turn_id=str(row["turn_id"]),
            owner_id=strict_int(row["owner_id"]),
            execution_host=str(row["execution_host"]),
            target_id=str(row["target_id"]),
            action_digest=str(row["action_digest"]),
            proposal_json=str(row["proposal_json"]),
            model_ref=str(row["model_ref"]),
            created_at=strict_int(row["created_at"]),
            updated_at=strict_int(row["updated_at"]),
            expires_at=strict_int(row["expires_at"]),
            status=str(row["status"]),
            decision=str(row["decision"]),
            reason_code=str(row["reason_code"]),
            worker_id=str(row["worker_id"]),
            input_tokens=strict_int(row["input_tokens"]),
            output_tokens=strict_int(row["output_tokens"]),
            pk=strict_int(row["id"]),
        )


class ProjectTaskReviewRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def get(conn: Any, execution_host: str, target_id: str) -> TaskReviewRow | None:
        row = conn.execute(
            "SELECT * FROM project_task_reviews WHERE execution_host=? AND target_id=?",
            (execution_host, target_id),
        ).fetchone()
        return TaskReviewRow.from_row(row) if row is not None else None

    def read(self, review_id: str, owner_id: int) -> TaskReviewRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM project_task_reviews WHERE review_id=? AND owner_id=?",
                (review_id, owner_id),
            ).fetchone()
        return TaskReviewRow.from_row(row) if row is not None else None

    @staticmethod
    def denied_client_arguments(conn: Any, turn_id: str, client_id: str, name: str) -> list[str]:
        rows = conn.execute(
            "SELECT c.arguments_json FROM project_task_reviews r "
            "JOIN project_task_tool_calls c ON c.call_id=r.target_id AND c.turn_id=r.turn_id "
            "WHERE r.execution_host='client' AND r.turn_id=? AND c.client_id=? AND c.name=? "
            "AND r.status='completed' AND r.decision='deny'",
            (turn_id, client_id, name),
        ).fetchall()
        return [str(row["arguments_json"]) for row in rows]

    @staticmethod
    def enqueue(conn: Any, row: TaskReviewRow) -> TaskReviewRow:
        conn.execute(
            "INSERT INTO project_task_reviews(review_id,task_id,turn_id,owner_id,"
            "execution_host,target_id,action_digest,proposal_json,model_ref,created_at,"
            "updated_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(execution_host,target_id) DO NOTHING",
            (
                row.review_id,
                row.task_id,
                row.turn_id,
                row.owner_id,
                row.execution_host,
                row.target_id,
                row.action_digest,
                row.proposal_json,
                row.model_ref,
                row.created_at,
                row.updated_at,
                row.expires_at,
            ),
        )
        saved = ProjectTaskReviewRepo.get(conn, row.execution_host, row.target_id)
        if saved is None:
            raise RuntimeError("Approval review insert did not persist")
        if (
            saved.task_id,
            saved.turn_id,
            saved.owner_id,
            saved.action_digest,
            saved.proposal_json,
            saved.model_ref,
        ) != (
            row.task_id,
            row.turn_id,
            row.owner_id,
            row.action_digest,
            row.proposal_json,
            row.model_ref,
        ):
            raise ValueError("Approval review target facts changed")
        return saved

    @staticmethod
    def cancel(conn: Any, turn_id: str, now: int) -> None:
        conn.execute(
            "UPDATE project_task_reviews SET status='cancelled',decision='',"
            "reason_code='',updated_at=? WHERE turn_id=? AND status IN ('queued','reviewing')",
            (now, turn_id),
        )

    def claim(self, worker_id: str, now: int) -> TaskReviewRow | None:
        with self._db.transaction() as conn:
            candidate = conn.execute(
                "SELECT * FROM project_task_reviews j WHERE j.status='queued' OR "
                "(j.status='reviewing' AND (j.updated_at<=? OR j.expires_at<=? OR NOT EXISTS ("
                "SELECT 1 FROM project_task_turns r JOIN users u ON u.id=j.owner_id "
                "WHERE r.turn_id=j.turn_id AND r.task_id=j.task_id AND u.disabled=0 "
                "AND r.cancel_requested=0 AND r.status IN ('queued','running')))) "
                "ORDER BY j.id LIMIT 1",
                (now - _LEASE_MS, now),
            ).fetchone()
            if candidate is None:
                return None
            # Match the lock order used by task cancellation and response handling:
            # owner first, then the review row. This also fences multiple processes.
            enabled = ProjectTaskRepo(self._db).lock_owner(conn, strict_int(candidate["owner_id"]))
            current = self.get(conn, str(candidate["execution_host"]), str(candidate["target_id"]))
            if current is None or current.status not in {"queued", "reviewing"}:
                return None
            active = conn.execute(
                "SELECT 1 FROM project_task_turns WHERE turn_id=? AND task_id=? "
                "AND cancel_requested=0 AND status IN ('queued','running')",
                (current.turn_id, current.task_id),
            ).fetchone()
            if not enabled or active is None:
                self.cancel(conn, current.turn_id, now)
                return None
            reason = ""
            if current.expires_at <= now:
                reason = "review_expired"
            elif current.status == "reviewing" and current.updated_at <= now - _LEASE_MS:
                reason = "review_unknown"
            if reason:
                # Keep the old worker id for possible late usage settlement.
                # Unknown dispatched requests must never be sent to the model again.
                conn.execute(
                    "UPDATE project_task_reviews SET status='completed',decision='requires_user',"
                    "reason_code=?,updated_at=? WHERE review_id=?",
                    (reason, now, current.review_id),
                )
                return None
            if current.status != "queued":
                return None
            claimed = conn.execute(
                "UPDATE project_task_reviews SET status='reviewing',worker_id=?,updated_at=? "
                "WHERE review_id=? AND status='queued' AND expires_at>? RETURNING *",
                (worker_id, now, current.review_id, now),
            ).fetchone()
            return TaskReviewRow.from_row(claimed) if claimed is not None else None

    def finish(
        self,
        job: TaskReviewRow,
        decision: str,
        reason_code: str,
        input_tokens: int,
        output_tokens: int,
        now: int,
    ) -> bool:
        with self._db.transaction() as conn:
            owner_enabled = ProjectTaskRepo(self._db).lock_owner(conn, job.owner_id)
            current = self.get(conn, job.execution_host, job.target_id)
            if current is None or not job.worker_id or current.worker_id != job.worker_id:
                return False
            active = conn.execute(
                "SELECT 1 FROM project_task_turns WHERE turn_id=? "
                "AND task_id=? AND cancel_requested=0 AND status IN ('queued','running')",
                (job.turn_id, job.task_id),
            ).fetchone()
            accepted = current.status == "reviewing" and owner_enabled and active is not None
            status = current.status
            if accepted:
                status = "completed"
                if now >= current.expires_at:
                    decision, reason_code = "requires_user", "review_expired"
                elif current.updated_at <= now - _LEASE_MS:
                    decision, reason_code = "requires_user", "review_unknown"
            elif current.status == "reviewing":
                status = "cancelled"
                decision, reason_code = "", ""
            else:
                decision, reason_code = current.decision, current.reason_code
            # Clear the worker id only when settling usage. Cancel/expiry recovery
            # retains it, allowing one late bill without restoring authorization.
            updated = conn.execute(
                "UPDATE project_task_reviews SET status=?,decision=?,reason_code=?,worker_id='',"
                "input_tokens=input_tokens+?,output_tokens=output_tokens+?,updated_at=? "
                "WHERE review_id=? AND worker_id=?",
                (
                    status,
                    decision,
                    reason_code,
                    max(input_tokens, 0),
                    max(output_tokens, 0),
                    now,
                    job.review_id,
                    job.worker_id,
                ),
            )
            return accepted and updated.rowcount == 1
