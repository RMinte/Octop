"""SQL journal for task inputs, streamed replies and worker leases."""

from __future__ import annotations

import builtins
import json
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int
from octop.infra.db.repos.project_tasks import ProjectTaskRepo
from octop.infra.db.repos.resident_delegations import ResidentTaskReportRepo


@dataclass(frozen=True)
class ProjectTaskTurnRow:
    turn_id: str
    task_id: str
    request_id: str
    prompt: str
    reply: str
    status: str
    error_code: str
    activity: str
    configuration_json: str
    model_ref: str
    worker_id: str
    cancel_requested: bool
    created_at: int
    updated_at: int
    input_tokens: int = 0
    output_tokens: int = 0
    pk: int = 0
    reasoning: str = ""
    content_blocks_json: str = "[]"
    content_revision: int = 0
    checkpoint_id: str = ""
    pause_json: str = ""
    interactions_json: str = "[]"
    interaction_revision: int = 0
    pause_requested: bool = False
    wait_kind: str = ""
    requests_json: str = "[]"
    started_at: int | None = None
    finished_at: int | None = None
    recovery_json: str = ""

    @property
    def is_waiting(self) -> bool:
        return (
            self.status == "running"
            and not self.worker_id
            and bool(self.checkpoint_id or self.wait_kind == "interrupted")
        )

    @classmethod
    def from_row(cls, r: DbRow) -> ProjectTaskTurnRow:
        return cls(
            turn_id=str(r["turn_id"]),
            task_id=str(r["task_id"]),
            request_id=str(r["request_id"]),
            prompt=str(r["prompt"]),
            reply=str(r["reply"]),
            status=str(r["status"]),
            error_code=str(r["error_code"]),
            activity=str(r["activity"]),
            configuration_json=str(r["configuration_json"]),
            model_ref=str(r["model_ref"]),
            worker_id=str(r["worker_id"]),
            cancel_requested=bool(r["cancel_requested"]),
            created_at=strict_int(r["created_at"]),
            updated_at=strict_int(r["updated_at"]),
            input_tokens=strict_int(r["input_tokens"]),
            output_tokens=strict_int(r["output_tokens"]),
            pk=strict_int(r["id"]),
            reasoning=str(r["reasoning"]),
            content_blocks_json=str(r["content_blocks_json"]),
            content_revision=strict_int(r["content_revision"]),
            checkpoint_id=str(r["checkpoint_id"]),
            pause_json=str(r["pause_json"]),
            interactions_json=str(r["interactions_json"]),
            interaction_revision=strict_int(r["interaction_revision"]),
            pause_requested=bool(r["pause_requested"]),
            wait_kind=str(r["wait_kind"]),
            requests_json=str(r["requests_json"]),
            started_at=strict_int(r["started_at"]) if r["started_at"] is not None else None,
            finished_at=strict_int(r["finished_at"]) if r["finished_at"] is not None else None,
            recovery_json=str(r["recovery_json"]),
        )


class ProjectTaskTurnRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def insert_imported(conn: Any, row: ProjectTaskTurnRow) -> None:
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,reply,status,"
            "configuration_json,model_ref,created_at,updated_at,content_blocks_json,"
            "content_revision,finished_at) VALUES (?,?,?,?,?,'completed',?,'',?,?,?,?,?)",
            (
                row.turn_id,
                row.task_id,
                row.request_id,
                row.prompt,
                row.reply,
                row.configuration_json,
                row.created_at,
                row.updated_at,
                row.content_blocks_json,
                row.content_revision,
                row.finished_at,
            ),
        )

    @staticmethod
    def update_imported(conn: Any, row: ProjectTaskTurnRow) -> None:
        conn.execute(
            "UPDATE project_task_turns SET reply=?,configuration_json=?,updated_at=?,"
            "content_blocks_json=?,content_revision=?,finished_at=? "
            "WHERE turn_id=? AND task_id=? AND status='completed'",
            (
                row.reply,
                row.configuration_json,
                row.updated_at,
                row.content_blocks_json,
                row.content_revision,
                row.finished_at,
                row.turn_id,
                row.task_id,
            ),
        )

    def list(self, task_id: str) -> builtins.list[ProjectTaskTurnRow]:
        with self._db.connect() as conn:
            return self.list_in_transaction(conn, task_id)

    def context(self, task_id: str) -> tuple[int, str] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT t.id, c.messages_json FROM project_task_contexts c "
                "JOIN project_task_turns t ON t.turn_id=c.turn_id WHERE c.task_id=?",
                (task_id,),
            ).fetchone()
        return (strict_int(row["id"]), str(row["messages_json"])) if row else None

    def save_context(self, turn_id: str, worker_id: str, messages_json: str) -> bool:
        with self._db.transaction() as conn:
            return self.save_context_in_transaction(conn, turn_id, worker_id, messages_json)

    @staticmethod
    def seed_imported_context_in_transaction(
        conn: Any, task_id: str, turn_id: str, messages_json: str
    ) -> bool:
        """Seed completed imported history once; never replace a worker's later context."""
        row = conn.execute(
            "SELECT configuration_json FROM project_task_turns "
            "WHERE task_id=? AND turn_id=? AND status='completed'",
            (task_id, turn_id),
        ).fetchone()
        if (
            row is None
            or json.loads(str(row["configuration_json"])).get("imported_history") is not True
        ):
            return False
        return bool(
            conn.execute(
                "INSERT INTO project_task_contexts(task_id,turn_id,messages_json) VALUES (?,?,?) "
                "ON CONFLICT(task_id) DO NOTHING",
                (task_id, turn_id, messages_json),
            ).rowcount
        )

    @staticmethod
    def save_context_in_transaction(
        conn: Any, turn_id: str, worker_id: str, messages_json: str
    ) -> bool:
        # Take the same lease row lock as progress/finalization. A stale worker
        # cannot overwrite context after recovery or a subsequent turn.
        row = conn.execute(
            "UPDATE project_task_turns SET worker_id=worker_id "
            "WHERE turn_id=? AND worker_id=? AND status='running' RETURNING task_id",
            (turn_id, worker_id),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "INSERT INTO project_task_contexts(task_id,turn_id,messages_json) VALUES (?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET turn_id=excluded.turn_id, messages_json=excluded.messages_json",
            (row["task_id"], turn_id, messages_json),
        )
        return True

    @staticmethod
    def list_in_transaction(conn: Any, task_id: str) -> builtins.list[ProjectTaskTurnRow]:
        rows = conn.execute(
            "SELECT * FROM project_task_turns WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
        return [ProjectTaskTurnRow.from_row(r) for r in rows]

    @staticmethod
    def page(
        conn: Any, task_id: str, limit: int, before: int | None = None
    ) -> builtins.list[ProjectTaskTurnRow]:
        position = " AND id<?" if before is not None else ""
        params = (task_id, before, limit) if before is not None else (task_id, limit)
        rows = conn.execute(
            "SELECT * FROM project_task_turns WHERE task_id=?"
            + position
            + " ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [ProjectTaskTurnRow.from_row(r) for r in reversed(rows)]

    @staticmethod
    def since(
        conn: Any, task_id: str, position: int, limit: int
    ) -> builtins.list[ProjectTaskTurnRow]:
        rows = conn.execute(
            "SELECT * FROM project_task_turns WHERE task_id=? AND id>=? ORDER BY id LIMIT ?",
            (task_id, position, limit),
        ).fetchall()
        return [ProjectTaskTurnRow.from_row(r) for r in rows]

    def get(self, turn_id: str) -> ProjectTaskTurnRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, turn_id)

    @staticmethod
    def get_in_transaction(conn: Any, turn_id: str) -> ProjectTaskTurnRow | None:
        r = conn.execute("SELECT * FROM project_task_turns WHERE turn_id=?", (turn_id,)).fetchone()
        return ProjectTaskTurnRow.from_row(r) if r else None

    @staticmethod
    def by_request(conn: Any, task_id: str, request_id: str) -> ProjectTaskTurnRow | None:
        r = conn.execute(
            "SELECT * FROM project_task_turns WHERE task_id=? AND request_id=?",
            (task_id, request_id),
        ).fetchone()
        return ProjectTaskTurnRow.from_row(r) if r else None

    @staticmethod
    def insert(conn: Any, row: ProjectTaskTurnRow) -> None:
        conn.execute(
            "INSERT INTO project_task_turns(turn_id, task_id, request_id, prompt, status, "
            "configuration_json, model_ref, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (
                row.turn_id,
                row.task_id,
                row.request_id,
                row.prompt,
                row.configuration_json,
                row.model_ref,
                row.created_at,
                row.updated_at,
            ),
        )

    def claim(self, worker_id: str, now: int) -> ProjectTaskTurnRow | None:
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT turn_id FROM project_task_turns WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            # A checkpoint predating timing support has an unknown first start;
            # resuming it must not invent that boundary from the current claim.
            r = conn.execute(
                "UPDATE project_task_turns SET status='running',wait_kind='', worker_id=?, updated_at=?, "
                "started_at=CASE WHEN checkpoint_id='' AND recovery_json='' "
                "THEN COALESCE(started_at,?) ELSE started_at END "
                "WHERE turn_id=? AND status='queued' RETURNING *",
                (worker_id, now, now, row["turn_id"]),
            ).fetchone()
            if r is None:
                return None
            turn = ProjectTaskTurnRow.from_row(r)
            ProjectTaskRepo.start(conn, turn.task_id, turn.turn_id, now)
            return turn

    def progress(
        self,
        turn_id: str,
        worker_id: str,
        reply: str,
        activity: str,
        now: int,
        *,
        reasoning: str | None = None,
        content_blocks_json: str | None = None,
        content_revision: int | None = None,
        require_active: bool = False,
    ) -> bool:
        with self._db.connect() as conn:
            active = " AND cancel_requested=0 AND pause_requested=0" if require_active else ""
            revision_args: tuple[int, ...] = ()
            if require_active and content_revision is not None:
                active += " AND content_revision<?"
                revision_args = (content_revision,)
            r = conn.execute(
                "UPDATE project_task_turns SET reply=?, activity=?, updated_at=?, reasoning=COALESCE(?,reasoning), "
                "content_blocks_json=COALESCE(?,content_blocks_json), content_revision=COALESCE(?,content_revision) "
                "WHERE turn_id=? AND worker_id=? AND status='running'"
                + active
                + " RETURNING cancel_requested",
                (
                    reply,
                    activity,
                    now,
                    reasoning,
                    content_blocks_json,
                    content_revision,
                    turn_id,
                    worker_id,
                    *revision_args,
                ),
            ).fetchone()
        return r is None or bool(r["cancel_requested"])

    @staticmethod
    def finish(
        conn: Any,
        turn_id: str,
        worker_id: str,
        status: str,
        reply: str,
        error_code: str,
        now: int,
        *,
        reasoning: str | None = None,
        content_blocks_json: str | None = None,
        content_revision: int | None = None,
        recovery_json: str | None = None,
    ) -> bool:
        changed = bool(
            conn.execute(
                "UPDATE project_task_turns SET status=CASE WHEN cancel_requested=1 "
                "THEN 'cancelled' ELSE ? END, reply=?, error_code=?, activity='', updated_at=?, "
                "finished_at=COALESCE(finished_at,?), reasoning=COALESCE(?,reasoning), "
                "content_blocks_json=COALESCE(?,content_blocks_json), content_revision=COALESCE(?,content_revision), "
                "recovery_json=COALESCE(?,recovery_json) "
                "WHERE turn_id=? AND worker_id=? AND status='running'",
                (
                    status,
                    reply,
                    error_code,
                    now,
                    now,
                    reasoning,
                    content_blocks_json,
                    content_revision,
                    recovery_json,
                    turn_id,
                    worker_id,
                ),
            ).rowcount
            == 1
        )
        if changed:
            ResidentTaskReportRepo.capture_terminal(conn, turn_id, now)
        return changed

    @staticmethod
    def cancel(conn: Any, turn_id: str, now: int) -> None:
        conn.execute(
            "UPDATE project_task_turns SET cancel_requested=1, updated_at=?, "
            "finished_at=CASE WHEN status='queued' OR (worker_id='' "
            "AND (checkpoint_id<>'' OR wait_kind='interrupted')) "
            "THEN COALESCE(finished_at,?) ELSE finished_at END, "
            "status=CASE WHEN status='queued' OR (worker_id='' "
            "AND (checkpoint_id<>'' OR wait_kind='interrupted')) "
            "THEN 'cancelled' ELSE status END WHERE turn_id=? AND status IN ('queued', 'running')",
            (now, now, turn_id),
        )
        ResidentTaskReportRepo.capture_terminal(conn, turn_id, now)

    @staticmethod
    def pause(
        conn: Any,
        turn_id: str,
        worker_id: str,
        checkpoint_id: str,
        pause_json: str,
        reply: str,
        reasoning: str,
        content_blocks_json: str,
        content_revision: int,
        now: int,
        wait_kind: str = "client_tool",
        interactions_json: str | None = None,
    ) -> bool:
        return bool(
            conn.execute(
                "UPDATE project_task_turns SET worker_id='',wait_kind=?,"
                "checkpoint_id=?,pause_json=?,reply=?,reasoning=?,content_blocks_json=?,"
                "content_revision=?,updated_at=?,interactions_json=COALESCE(?,interactions_json),"
                "interaction_revision=interaction_revision+CASE WHEN ? IS NULL THEN 0 ELSE 1 END "
                "WHERE turn_id=? AND worker_id=? "
                "AND status='running' AND cancel_requested=0",
                (
                    wait_kind,
                    checkpoint_id,
                    pause_json,
                    reply,
                    reasoning,
                    content_blocks_json,
                    content_revision,
                    now,
                    interactions_json,
                    interactions_json,
                    turn_id,
                    worker_id,
                ),
            ).rowcount
            == 1
        )

    @staticmethod
    def save_interactions(conn: Any, turn_id: str, interactions_json: str, now: int) -> None:
        conn.execute(
            "UPDATE project_task_turns SET interactions_json=?,interaction_revision=interaction_revision+1,updated_at=? WHERE turn_id=?",
            (interactions_json, now, turn_id),
        )

    @staticmethod
    def save_request(conn: Any, turn_id: str, requests_json: str, pause_requested: bool) -> None:
        conn.execute(
            "UPDATE project_task_turns SET requests_json=?,pause_requested=?,interaction_revision=interaction_revision+1 WHERE turn_id=?",
            (requests_json, int(pause_requested), turn_id),
        )

    def waiting(self) -> builtins.list[ProjectTaskTurnRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM project_task_turns WHERE status='running' "
                "AND worker_id='' AND checkpoint_id<>'' AND wait_kind<>'interrupted' ORDER BY id"
            ).fetchall()
        return [ProjectTaskTurnRow.from_row(row) for row in rows]

    @staticmethod
    def resume_wait(
        conn: Any, turn_id: str, now: int, error: str = "", recovery_json: str | None = None
    ) -> bool:
        changed = bool(
            conn.execute(
                "UPDATE project_task_turns SET status=?,error_code=?,updated_at=?, "
                "recovery_json=COALESCE(?,recovery_json), "
                "finished_at=CASE WHEN ?<>'' THEN COALESCE(finished_at,?) ELSE finished_at END "
                "WHERE turn_id=? AND status='running' AND worker_id='' "
                "AND checkpoint_id<>'' AND wait_kind<>'interrupted' AND cancel_requested=0",
                ("failed" if error else "queued", error, now, recovery_json, error, now, turn_id),
            ).rowcount
            == 1
        )
        if changed and error:
            ResidentTaskReportRepo.capture_terminal(conn, turn_id, now)
        return changed

    @staticmethod
    def save_checkpoint(
        conn: Any, turn_id: str, worker_id: str, checkpoint_id: str, now: int
    ) -> bool:
        return bool(
            conn.execute(
                "UPDATE project_task_turns SET checkpoint_id=?,updated_at=? "
                "WHERE turn_id=? AND worker_id=? AND worker_id<>'' "
                "AND status='running' AND cancel_requested=0",
                (checkpoint_id, now, turn_id, worker_id),
            ).rowcount
            == 1
        )

    @staticmethod
    def park_recovery(
        conn: Any,
        turn_id: str,
        worker_id: str,
        checkpoint_id: str,
        recovery_json: str,
        now: int,
        *,
        before: int | None = None,
    ) -> bool:
        return bool(
            conn.execute(
                "UPDATE project_task_turns SET worker_id='',wait_kind='interrupted',"
                "checkpoint_id=?,recovery_json=?,activity='',updated_at=?,"
                "interaction_revision=interaction_revision+1 "
                "WHERE turn_id=? AND worker_id=? AND worker_id<>'' "
                "AND status='running' AND cancel_requested=0 "
                "AND (CAST(? AS BIGINT) IS NULL OR updated_at<?)",
                (checkpoint_id, recovery_json, now, turn_id, worker_id, before, before),
            ).rowcount
            == 1
        )

    @staticmethod
    def update_recovery(conn: Any, turn_id: str, recovery_json: str, now: int) -> bool:
        return bool(
            conn.execute(
                "UPDATE project_task_turns SET recovery_json=?,updated_at=?,"
                "interaction_revision=interaction_revision+1 "
                "WHERE turn_id=? AND status='running' AND worker_id='' "
                "AND wait_kind='interrupted' AND cancel_requested=0",
                (recovery_json, now, turn_id),
            ).rowcount
            == 1
        )

    @staticmethod
    def interrupt_wait(
        conn: Any, turn_id: str, checkpoint_id: str, recovery_json: str, now: int
    ) -> bool:
        return bool(
            conn.execute(
                "UPDATE project_task_turns SET wait_kind='interrupted',recovery_json=?,updated_at=?,"
                "interaction_revision=interaction_revision+1 "
                "WHERE turn_id=? AND status='running' AND worker_id='' "
                "AND checkpoint_id=? AND checkpoint_id<>'' "
                "AND wait_kind<>'interrupted' AND cancel_requested=0",
                (recovery_json, now, turn_id, checkpoint_id),
            ).rowcount
            == 1
        )

    @staticmethod
    def resume_recovery(conn: Any, turn_id: str, now: int) -> bool:
        return bool(
            conn.execute(
                "UPDATE project_task_turns SET status='queued',wait_kind='',updated_at=?,"
                "interaction_revision=interaction_revision+1 "
                "WHERE turn_id=? AND status='running' AND worker_id='' "
                "AND wait_kind='interrupted' AND cancel_requested=0",
                (now, turn_id),
            ).rowcount
            == 1
        )

    def stale(self, before: int) -> builtins.list[ProjectTaskTurnRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM project_task_turns WHERE status='running' AND updated_at<? "
                "AND NOT (worker_id='' AND (checkpoint_id<>'' OR wait_kind='interrupted'))",
                (before,),
            ).fetchall()
        return [ProjectTaskTurnRow.from_row(r) for r in rows]

    @staticmethod
    def interrupt(conn: Any, turn_id: str, before: int, now: int) -> bool:
        changed = bool(
            conn.execute(
                "UPDATE project_task_turns SET status='failed', error_code='TASK_INTERRUPTED', "
                "activity='', updated_at=?,finished_at=COALESCE(finished_at,?) "
                "WHERE turn_id=? AND status='running' AND updated_at<? "
                "AND NOT (worker_id='' AND (checkpoint_id<>'' OR wait_kind='interrupted'))",
                (now, now, turn_id, before),
            ).rowcount
            == 1
        )
        if changed:
            ResidentTaskReportRepo.capture_terminal(conn, turn_id, now)
        return changed

    def record_usage(self, turn_id: str, input_tokens: int, output_tokens: int) -> None:
        """Add known request usage, including a cancelled request's late receipt."""
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE project_task_turns SET input_tokens=input_tokens+?, "
                "output_tokens=output_tokens+? WHERE turn_id=?",
                (input_tokens, output_tokens, turn_id),
            )
