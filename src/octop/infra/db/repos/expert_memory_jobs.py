"""Durable expert-memory work receipts; no model or memory-store dependencies."""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int


@dataclass(frozen=True)
class ExpertMemoryJob:
    job_id: str
    owner_id: int
    expert_id: str
    task_id: str
    turn_id: str
    namespace: str
    payload_json: str
    status: str
    phase: str
    result_json: str
    worker_id: str
    error_code: str

    @classmethod
    def from_row(cls, row: DbRow) -> ExpertMemoryJob:
        return cls(
            job_id=str(row["job_id"]),
            owner_id=strict_int(row["owner_id"]),
            **{
                key: str(row[key])
                for key in (
                    "expert_id",
                    "task_id",
                    "turn_id",
                    "namespace",
                    "payload_json",
                    "status",
                    "phase",
                    "result_json",
                    "worker_id",
                    "error_code",
                )
            },
        )


class ExpertMemoryJobRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def enqueue(
        self,
        *,
        job_id: str,
        owner_id: int,
        expert_id: str,
        task_id: str,
        turn_id: str,
        namespace: str,
        payload_json: str,
        now: int,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO expert_memory_jobs(job_id,owner_id,expert_id,task_id,turn_id,"
                "namespace,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(job_id) DO NOTHING",
                (job_id, owner_id, expert_id, task_id, turn_id, namespace, payload_json, now, now),
            )

    def get(self, job_id: str) -> ExpertMemoryJob | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM expert_memory_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return ExpertMemoryJob.from_row(row) if row is not None else None

    def list_for_task(self, task_id: str, owner_id: int, limit: int = 20) -> list[ExpertMemoryJob]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM expert_memory_jobs WHERE task_id=? AND owner_id=? "
                "ORDER BY created_at DESC,job_id DESC LIMIT ?",
                (task_id, owner_id, limit),
            ).fetchall()
        return [ExpertMemoryJob.from_row(row) for row in rows]

    def claim(self, worker_id: str, now: int) -> ExpertMemoryJob | None:
        with self._db.transaction() as conn:
            # A provider outcome without a durable response is unknown. Replaying
            # it can duplicate billing or data; only persisted proposals resume.
            conn.execute(
                "UPDATE expert_memory_jobs SET status='uncertain',error_code='unknown_outcome',"
                "worker_id='',updated_at=? WHERE status='running' AND lease_until<=? "
                "AND phase='extracting'",
                (now, now),
            )
            row = conn.execute(
                "SELECT j.job_id FROM expert_memory_jobs j JOIN project_task_turns r "
                "ON r.turn_id=j.turn_id WHERE (j.status='pending' OR "
                "(j.status='running' AND j.lease_until<=? AND j.phase IN ('queued','ready','applying'))) "
                "AND r.status IN ('completed','cancelled','failed') ORDER BY j.created_at,j.job_id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            claimed = conn.execute(
                "UPDATE expert_memory_jobs SET status='running',worker_id=?,lease_until=?,"
                "updated_at=? WHERE job_id=? AND (status='pending' OR "
                "(status='running' AND lease_until<=?)) RETURNING *",
                (worker_id, now + 120_000, now, row["job_id"], now),
            ).fetchone()
        return ExpertMemoryJob.from_row(claimed) if claimed is not None else None

    def update(
        self,
        job: ExpertMemoryJob,
        *,
        status: str = "running",
        phase: str | None = None,
        result_json: str | None = None,
        error_code: str = "",
        now: int,
    ) -> bool:
        with self._db.transaction() as conn:
            result = conn.execute(
                "UPDATE expert_memory_jobs SET status=?,phase=COALESCE(?,phase),"
                "result_json=COALESCE(?,result_json),error_code=?,updated_at=?,lease_until=? "
                "WHERE job_id=? AND worker_id=? AND status='running'",
                (
                    status,
                    phase,
                    result_json,
                    error_code,
                    now,
                    now + 120_000,
                    job.job_id,
                    job.worker_id,
                ),
            )
        return bool(result.rowcount == 1)

    def heartbeat(self, job: ExpertMemoryJob, now: int) -> bool:
        return self.update(job, now=now)
