"""SQL-only title generation jobs, leases and usage for independent tasks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import strict_int
from octop.infra.db.repos.project_tasks import ProjectTaskRepo


@dataclass(frozen=True)
class TaskTitleJob:
    task_id: str
    owner_id: int
    title_revision: int
    prompt: str
    model_ref: str
    worker_id: str
    attempts: int


class ProjectTaskTitleRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def enqueue(conn: Any, task_id: str) -> None:
        conn.execute("INSERT INTO project_task_titles(task_id) VALUES (?)", (task_id,))

    @staticmethod
    def skip_pending(conn: Any, task_id: str) -> None:
        conn.execute(
            "UPDATE project_task_titles SET status='skipped' WHERE task_id=? AND status='pending'",
            (task_id,),
        )

    def claim(self, worker_id: str, now: int) -> TaskTitleJob | None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE project_task_titles SET status='failed' "
                "WHERE status='running' AND lease_until<=? AND attempts>=2",
                (now,),
            )
            candidate = conn.execute(
                "SELECT j.task_id, j.title_revision, t.owner_id, r.prompt, r.model_ref "
                "FROM project_task_titles j JOIN project_tasks t ON t.task_id=j.task_id "
                "JOIN users u ON u.id=t.owner_id "
                "JOIN project_task_turns r ON r.task_id=t.task_id AND r.request_id=t.request_id "
                "WHERE j.status IN ('pending', 'running') AND j.lease_until<=? AND j.attempts<2 "
                "AND t.title_source='provisional' AND t.title_revision=j.title_revision "
                "AND u.disabled=0 AND (r.status IN ('completed', 'failed', 'cancelled') "
                "OR (r.status='running' AND r.reply<>'')) ORDER BY r.id LIMIT 1",
                (now,),
            ).fetchone()
            if candidate is None:
                return None
            claimed = conn.execute(
                "UPDATE project_task_titles SET status='running', worker_id=?, "
                "attempts=attempts+1, lease_until=? WHERE task_id=? "
                "AND status IN ('pending', 'running') AND lease_until<=? AND attempts<2 "
                "RETURNING attempts",
                (worker_id, now + 60_000, candidate["task_id"], now),
            ).fetchone()
            if claimed is None:
                return None
            return TaskTitleJob(
                str(candidate["task_id"]),
                strict_int(candidate["owner_id"]),
                strict_int(candidate["title_revision"]),
                str(candidate["prompt"]),
                str(candidate["model_ref"]),
                worker_id,
                strict_int(claimed["attempts"]),
            )

    def finish(
        self,
        job: TaskTitleJob,
        title: str | None,
        input_tokens: int,
        output_tokens: int,
        now: int,
    ) -> None:
        with self._db.transaction() as conn:
            owner_enabled = ProjectTaskRepo(self._db).lock_owner(conn, job.owner_id)
            # The attempt check also rejects a late response from the same worker's old lease.
            updated = conn.execute(
                "UPDATE project_task_titles SET status=?, lease_until=?, "
                "input_tokens=input_tokens+?, output_tokens=output_tokens+? "
                "WHERE task_id=? AND status='running' AND worker_id=? AND attempts=?",
                (
                    "completed" if title else ("pending" if job.attempts < 2 else "failed"),
                    now + 5_000,
                    input_tokens,
                    output_tokens,
                    job.task_id,
                    job.worker_id,
                    job.attempts,
                ),
            )
            if updated.rowcount != 1:
                return
            if not title:
                conn.execute(
                    "UPDATE project_task_titles SET status='skipped' WHERE task_id=? AND EXISTS ("
                    "SELECT 1 FROM project_tasks WHERE task_id=? "
                    "AND (title_source<>'provisional' OR title_revision<>?))",
                    (job.task_id, job.task_id, job.title_revision),
                )
            if title and (
                not owner_enabled
                or not ProjectTaskRepo.apply_generated_title(
                    conn, job.task_id, job.owner_id, job.title_revision, title
                )
            ):
                conn.execute(
                    "UPDATE project_task_titles SET status='skipped' WHERE task_id=?",
                    (job.task_id,),
                )
