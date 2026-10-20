"""SQL-only delegation receipts and immutable task-result outbox records."""

from __future__ import annotations

import builtins
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int
from octop.infra.db.repos.resident_inbox import ResidentInboxRepo


@dataclass(frozen=True)
class ResidentDelegationRow:
    delegation_id: str
    owner_id: int
    origin_agent_id: str
    origin_thread_id: str
    source_turn_id: str
    task_id: str
    initial_turn_id: str
    request_id: str
    fingerprint: str
    timezone: str
    created_at: int
    updated_at: int
    pk: int = 0

    @classmethod
    def from_row(cls, row: DbRow) -> ResidentDelegationRow:
        return cls(
            delegation_id=str(row["delegation_id"]),
            owner_id=strict_int(row["owner_id"]),
            origin_agent_id=str(row["origin_agent_id"]),
            origin_thread_id=str(row["origin_thread_id"]),
            source_turn_id=str(row["source_turn_id"]),
            task_id=str(row["task_id"]),
            initial_turn_id=str(row["initial_turn_id"]),
            request_id=str(row["request_id"]),
            fingerprint=str(row["fingerprint"]),
            timezone=str(row["timezone"]),
            created_at=strict_int(row["created_at"]),
            updated_at=strict_int(row["updated_at"]),
            pk=strict_int(row["id"]),
        )


@dataclass(frozen=True)
class ResidentTaskReportRow:
    report_id: str
    message_id: str
    delegation_id: str
    owner_id: int
    thread_id: str
    task_id: str
    task_turn_id: str
    status: str
    text: str
    error_code: str
    artifacts_json: str
    day_key: str
    timezone: str
    captured_at: int
    created_at: int
    updated_at: int
    delivery_status: str
    archive_written_at: int | None
    context_written_at: int | None
    delivery_error: str
    pk: int = 0

    @classmethod
    def from_row(cls, row: DbRow) -> ResidentTaskReportRow:
        return cls(
            report_id=str(row["report_id"]),
            message_id=str(row["message_id"]),
            delegation_id=str(row["delegation_id"]),
            owner_id=strict_int(row["owner_id"]),
            thread_id=str(row["thread_id"]),
            task_id=str(row["task_id"]),
            task_turn_id=str(row["task_turn_id"]),
            status=str(row["status"]),
            text=str(row["text"]),
            error_code=str(row["error_code"]),
            artifacts_json=str(row["artifacts_json"]),
            day_key=str(row["day_key"]),
            timezone=str(row["timezone"]),
            captured_at=strict_int(row["captured_at"]),
            created_at=strict_int(row["created_at"]),
            updated_at=strict_int(row["updated_at"]),
            delivery_status=str(row["delivery_status"]),
            archive_written_at=(
                strict_int(row["archive_written_at"])
                if row["archive_written_at"] is not None
                else None
            ),
            context_written_at=(
                strict_int(row["context_written_at"])
                if row["context_written_at"] is not None
                else None
            ),
            delivery_error=str(row["delivery_error"]),
            pk=strict_int(row["id"]),
        )


class ResidentDelegationRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def by_request(conn: Any, owner_id: int, request_id: str) -> ResidentDelegationRow | None:
        row = conn.execute(
            "SELECT * FROM resident_delegations WHERE owner_id=? AND request_id=?",
            (owner_id, request_id),
        ).fetchone()
        return ResidentDelegationRow.from_row(row) if row else None

    @staticmethod
    def get_in_transaction(
        conn: Any, owner_id: int, delegation_id: str
    ) -> ResidentDelegationRow | None:
        row = conn.execute(
            "SELECT * FROM resident_delegations WHERE owner_id=? AND delegation_id=?",
            (owner_id, delegation_id),
        ).fetchone()
        return ResidentDelegationRow.from_row(row) if row else None

    def get(self, owner_id: int, delegation_id: str) -> ResidentDelegationRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, owner_id, delegation_id)

    def latest_linked_turn(self, conn: Any, row: ResidentDelegationRow) -> DbRow | None:
        retry_of = (
            "CAST(t.configuration_json AS JSONB)->>'retry_of_turn_id'"
            if self._db.dialect == "postgresql"
            else "json_extract(t.configuration_json,'$.retry_of_turn_id')"
        )
        result = conn.execute(
            "WITH RECURSIVE linked(turn_id,id) AS ("
            "SELECT turn_id,id FROM project_task_turns WHERE task_id=? AND turn_id=? "
            "UNION ALL SELECT t.turn_id,t.id FROM project_task_turns t JOIN linked l ON "
            + retry_of
            + "=l.turn_id WHERE t.task_id=? AND t.id>l.id) "
            "SELECT turn_id,status,wait_kind,pause_requested,checkpoint_id,worker_id "
            "FROM project_task_turns WHERE turn_id=(SELECT turn_id FROM linked ORDER BY id DESC LIMIT 1)",
            (row.task_id, row.initial_turn_id, row.task_id),
        ).fetchone()
        return cast(DbRow | None, result)

    @staticmethod
    def insert(conn: Any, row: ResidentDelegationRow) -> None:
        conn.execute(
            "INSERT INTO resident_delegations(delegation_id,owner_id,origin_agent_id,"
            "origin_thread_id,source_turn_id,task_id,initial_turn_id,request_id,fingerprint,"
            "timezone,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.delegation_id,
                row.owner_id,
                row.origin_agent_id,
                row.origin_thread_id,
                row.source_turn_id,
                row.task_id,
                row.initial_turn_id,
                row.request_id,
                row.fingerprint,
                row.timezone,
                row.created_at,
                row.updated_at,
            ),
        )

    @staticmethod
    def page(
        conn: Any,
        owner_id: int,
        thread_id: str,
        limit: int,
        before: int | None = None,
    ) -> builtins.list[ResidentDelegationRow]:
        boundary = " AND id<?" if before is not None else ""
        params = (
            (owner_id, thread_id, before, limit)
            if before is not None
            else (owner_id, thread_id, limit)
        )
        rows = conn.execute(
            "SELECT * FROM resident_delegations WHERE owner_id=? AND origin_thread_id=?"
            + boundary
            + " ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [ResidentDelegationRow.from_row(row) for row in rows]


class ResidentTaskReportRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def for_delegation(
        conn: Any, owner_id: int, delegation_id: str
    ) -> builtins.list[ResidentTaskReportRow]:
        rows = conn.execute(
            "SELECT * FROM resident_task_reports WHERE owner_id=? AND delegation_id=? ORDER BY id",
            (owner_id, delegation_id),
        ).fetchall()
        return [ResidentTaskReportRow.from_row(row) for row in rows]

    def get(self, owner_id: int, report_id: str) -> ResidentTaskReportRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM resident_task_reports WHERE owner_id=? AND report_id=?",
                (owner_id, report_id),
            ).fetchone()
        return ResidentTaskReportRow.from_row(row) if row else None

    def pending(self, limit: int = 100) -> builtins.list[ResidentTaskReportRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM resident_task_reports WHERE delivery_status='pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [ResidentTaskReportRow.from_row(row) for row in rows]

    def pending_for_thread(
        self, owner_id: int, thread_id: str, limit: int = 100
    ) -> builtins.list[ResidentTaskReportRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM resident_task_reports WHERE owner_id=? AND thread_id=? "
                "AND delivery_status='pending' ORDER BY id LIMIT ?",
                (owner_id, thread_id, limit),
            ).fetchall()
        return [ResidentTaskReportRow.from_row(row) for row in rows]

    def prepare_message(self, report_id: str, wire_json: str) -> str:
        """Freeze the canonical wire once, before either external delivery write."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE resident_task_reports SET message_json=? WHERE report_id=? AND message_json=''",
                (wire_json, report_id),
            )
            row = conn.execute(
                "SELECT message_json FROM resident_task_reports WHERE report_id=?", (report_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Resident task report is unavailable")
            return str(row["message_json"])

    @staticmethod
    def capture_terminal(conn: Any, turn_id: str, now: int) -> None:
        """Capture only the delegated turn and its explicit retry ancestry, atomically."""
        turn = conn.execute(
            "SELECT * FROM project_task_turns WHERE turn_id=?", (turn_id,)
        ).fetchone()
        if turn is None or turn["status"] not in {"completed", "failed", "cancelled"}:
            return
        current = turn
        seen: set[str] = set()
        delegation = None
        while current is not None and str(current["turn_id"]) not in seen:
            current_id = str(current["turn_id"])
            seen.add(current_id)
            delegation = conn.execute(
                "SELECT * FROM resident_delegations WHERE initial_turn_id=? AND task_id=?",
                (current_id, turn["task_id"]),
            ).fetchone()
            if delegation is not None:
                break
            retry_of = json.loads(str(current["configuration_json"])).get("retry_of_turn_id")
            if not isinstance(retry_of, str):
                return
            current = conn.execute(
                "SELECT * FROM project_task_turns WHERE turn_id=? AND task_id=?",
                (retry_of, turn["task_id"]),
            ).fetchone()
        if delegation is None:
            return
        artifact_rows = conn.execute(
            "SELECT artifact_id,name,media_type,byte_count,sha256,business_category "
            "FROM project_task_artifacts WHERE task_id=? AND turn_id=? ORDER BY id",
            (turn["task_id"], turn_id),
        ).fetchall()
        artifact_refs = [
            {
                "artifact_id": str(row["artifact_id"]),
                "name": str(row["name"]),
                "media_type": str(row["media_type"]),
                "byte_count": strict_int(row["byte_count"]),
                "sha256": str(row["sha256"]),
                "business_category": str(row["business_category"]),
            }
            for row in artifact_rows
        ]
        timezone = str(delegation["timezone"])
        day_key = datetime.fromtimestamp(now / 1000, UTC).astimezone(ZoneInfo(timezone)).date()
        inserted = conn.execute(
            "INSERT INTO resident_task_reports(report_id,message_id,delegation_id,owner_id,"
            "thread_id,task_id,task_turn_id,status,text,error_code,artifacts_json,day_key,"
            "timezone,captured_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_turn_id) DO NOTHING",
            (
                "report_" + turn_id,
                "resident:report:" + turn_id,
                delegation["delegation_id"],
                delegation["owner_id"],
                delegation["origin_thread_id"],
                turn["task_id"],
                turn_id,
                turn["status"],
                str(turn["reply"])[:8000],
                "" if turn["status"] == "cancelled" else turn["error_code"],
                json.dumps(artifact_refs, ensure_ascii=False),
                day_key.isoformat(),
                timezone,
                now,
                now,
                now,
            ),
        ).rowcount
        if inserted:
            conn.execute(
                "UPDATE resident_conversations SET revision=revision+1,updated_at=? WHERE thread_id=?",
                (now, delegation["origin_thread_id"]),
            )

    def mark_archived(self, report_id: str, now: int) -> bool:
        return self._mark_stage(report_id, "archive_written_at", now)

    def mark_context(self, report_id: str, now: int) -> bool:
        return self._mark_stage(report_id, "context_written_at", now)

    def _mark_stage(self, report_id: str, column: str, now: int) -> bool:
        with self._db.transaction() as conn:
            row = conn.execute(
                f"UPDATE resident_task_reports SET {column}=?,updated_at=?,delivery_error='' "
                f"WHERE report_id=? AND {column} IS NULL RETURNING thread_id",
                (now, now, report_id),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE resident_conversations SET revision=revision+1,updated_at=? WHERE thread_id=?",
                    (now, row["thread_id"]),
                )
            return row is not None

    def mark_delivered(self, report_id: str, now: int) -> bool:
        with self._db.transaction() as conn:
            row = conn.execute(
                "UPDATE resident_task_reports SET delivery_status='delivered',delivery_error='',updated_at=? "
                "WHERE report_id=? AND delivery_status='pending' AND archive_written_at IS NOT NULL "
                "AND context_written_at IS NOT NULL RETURNING thread_id,owner_id,message_id",
                (now, report_id),
            ).fetchone()
            if row:
                ResidentInboxRepo.record(
                    conn,
                    strict_int(row["owner_id"]),
                    str(row["thread_id"]),
                    "task_report",
                    report_id,
                    str(row["message_id"]),
                    now,
                )
                conn.execute(
                    "UPDATE resident_conversations SET revision=revision+1,updated_at=? WHERE thread_id=?",
                    (now, row["thread_id"]),
                )
            return row is not None

    def record_delivery_error(self, report_id: str, error_code: str, now: int) -> None:
        with self._db.transaction() as conn:
            row = conn.execute(
                "UPDATE resident_task_reports SET delivery_error=?,updated_at=? "
                "WHERE report_id=? AND delivery_status='pending' AND delivery_error<>? RETURNING thread_id",
                (error_code, now, report_id, error_code),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE resident_conversations SET revision=revision+1,updated_at=? WHERE thread_id=?",
                    (now, row["thread_id"]),
                )
