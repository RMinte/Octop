"""Bounded read projection for resident turns, task reports and proactive messages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int


@dataclass(frozen=True)
class ResidentHistoryRow:
    item_id: str
    kind: str
    source_id: str
    day_key: str
    timezone: str
    created_at: int
    updated_at: int
    status: str
    prompt: str
    reply: str
    error_code: str
    request_id: str
    task_id: str
    task_turn_id: str
    message_id: str
    artifacts_json: str
    delivery_status: str
    followup_id: str = ""
    notification_kind: str = ""
    payload_json: str = "{}"
    input_json: str = "{}"

    @classmethod
    def from_row(cls, row: DbRow) -> ResidentHistoryRow:
        return cls(
            item_id=str(row["item_id"]),
            kind=str(row["kind"]),
            source_id=str(row["source_id"]),
            day_key=str(row["day_key"]),
            timezone=str(row["timezone"]),
            created_at=strict_int(row["created_at"]),
            updated_at=strict_int(row["updated_at"]),
            status=str(row["status"]),
            prompt=str(row["prompt"]),
            reply=str(row["reply"]),
            error_code=str(row["error_code"]),
            request_id=str(row["request_id"]),
            task_id=str(row["task_id"]),
            task_turn_id=str(row["task_turn_id"]),
            message_id=str(row["message_id"]),
            artifacts_json=str(row["artifacts_json"]),
            delivery_status=str(row["delivery_status"]),
            followup_id=str(row["followup_id"]),
            notification_kind=str(row["notification_kind"]),
            payload_json=str(row["payload_json"]),
            input_json=str(row["input_json"]),
        )


# Each branch applies scope, keyset and LIMIT before the bounded windows are
# merged. A long thread never materializes all old message bodies for page 1.
_TURN = """SELECT 'turn:' || turn_id AS item_id,'turn' AS kind,turn_id AS source_id,
day_key,timezone,created_at,updated_at,status,prompt,reply,error_code,request_id,
'' AS task_id,'' AS task_turn_id,'' AS message_id,'[]' AS artifacts_json,
'' AS delivery_status,'' AS followup_id,'' AS notification_kind,'{}' AS payload_json,input_json
FROM resident_requests WHERE owner_id=? AND thread_id=?"""
_REPORT = """SELECT 'report:' || report_id AS item_id,'task_report' AS kind,report_id AS source_id,
day_key,timezone,captured_at AS created_at,updated_at,status,'' AS prompt,text AS reply,
error_code,'' AS request_id,task_id,task_turn_id,message_id,artifacts_json,delivery_status,
'' AS followup_id,'' AS notification_kind,'{}' AS payload_json,'{}' AS input_json
FROM resident_task_reports WHERE owner_id=? AND thread_id=?"""
_NOTIFICATION = """SELECT 'notification:' || notification_id AS item_id,'notification' AS kind,
notification_id AS source_id,day_key,timezone,created_at,updated_at,status,'' AS prompt,
text AS reply,'' AS error_code,'' AS request_id,'' AS task_id,'' AS task_turn_id,
message_id,'[]' AS artifacts_json,status AS delivery_status,followup_id,
kind AS notification_kind,payload_json,'{}' AS input_json FROM resident_notifications
WHERE owner_id=? AND thread_id=? AND status='delivered'"""
_TABLES = (
    ("resident_requests", ""),
    ("resident_task_reports", ""),
    ("resident_notifications", " AND status='delivered'"),
)


class ResidentHistoryRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def backfill_days(self, timezone: str) -> None:
        """Freeze a best-known day once for receipts predating day metadata."""
        zone = ZoneInfo(timezone)
        while True:
            with self._db.transaction() as conn:
                rows = conn.execute(
                    "SELECT id,created_at FROM resident_requests WHERE day_key='' ORDER BY id LIMIT 500"
                ).fetchall()
                if not rows:
                    return
                for row in rows:
                    day = (
                        datetime.fromtimestamp(strict_int(row["created_at"]) / 1000, UTC)
                        .astimezone(zone)
                        .date()
                        .isoformat()
                    )
                    conn.execute(
                        "UPDATE resident_requests SET day_key=?,timezone=? WHERE id=? AND day_key=''",
                        (day, timezone, row["id"]),
                    )

    @staticmethod
    def _revision(conn: Any, owner_id: int, thread_id: str) -> int:
        row = conn.execute(
            "SELECT revision FROM resident_conversations WHERE owner_id=? AND thread_id=?",
            (owner_id, thread_id),
        ).fetchone()
        return strict_int(row["revision"]) if row is not None else 0

    def days(
        self, owner_id: int, thread_id: str, limit: int, before: str = "", at: str = ""
    ) -> tuple[int, list[tuple[str, int, list[str]]]]:
        where = " AND day_key<>''"
        extra: list[Any] = []
        if before:
            where += " AND day_key<?"
            extra.append(before)
        if at:
            where += " AND day_key<=?"
            extra.append(at)
        with self._db.read_snapshot() as conn:
            revision = self._revision(conn, owner_id, thread_id)
            candidates: set[str] = set()
            for table, visible in _TABLES:
                rows = conn.execute(
                    f"SELECT DISTINCT day_key FROM {table} WHERE owner_id=? AND thread_id=?"
                    + visible
                    + where
                    + " ORDER BY day_key DESC LIMIT ?",
                    (owner_id, thread_id, *extra, limit),
                ).fetchall()
                candidates.update(str(row["day_key"]) for row in rows)
            days: list[tuple[str, int, list[str]]] = []
            for day in sorted(candidates, reverse=True)[:limit]:
                count = 0
                zones: set[str] = set()
                for table, visible in _TABLES:
                    scope = (
                        f" FROM {table} WHERE owner_id=? AND thread_id=? AND day_key=?" + visible
                    )
                    row = conn.execute(
                        "SELECT COUNT(*) AS item_count" + scope, (owner_id, thread_id, day)
                    ).fetchone()
                    count += strict_int(row["item_count"])
                    rows = conn.execute(
                        "SELECT DISTINCT timezone" + scope, (owner_id, thread_id, day)
                    ).fetchall()
                    zones.update(str(row["timezone"]) for row in rows)
                days.append((day, count, sorted(zones)))
            return revision, days

    def page(
        self,
        owner_id: int,
        thread_id: str,
        limit: int,
        *,
        day_key: str = "",
        query: str = "",
        before: tuple[int, str] | None = None,
        after: tuple[int, str] | None = None,
        item_id: str = "",
    ) -> tuple[int, list[ResidentHistoryRow]]:
        with self._db.read_snapshot() as conn:
            return self._revision(conn, owner_id, thread_id), self._page(
                conn,
                owner_id,
                thread_id,
                limit,
                day_key=day_key,
                query=query,
                before=before,
                after=after,
                item_id=item_id,
            )

    def _page(
        self,
        conn: Any,
        owner_id: int,
        thread_id: str,
        limit: int,
        *,
        day_key: str = "",
        query: str = "",
        before: tuple[int, str] | None = None,
        after: tuple[int, str] | None = None,
        item_id: str = "",
    ) -> list[ResidentHistoryRow]:
        common: list[str] = []
        common_values: list[Any] = []
        if day_key:
            common.append("day_key=?")
            common_values.append(day_key)
        direction = "ASC" if after else "DESC"
        combined: list[ResidentHistoryRow] = []
        for select, prefix, time_column, id_column in (
            (_TURN, "turn:", "created_at", "turn_id"),
            (_REPORT, "report:", "captured_at", "report_id"),
            (_NOTIFICATION, "notification:", "created_at", "notification_id"),
        ):
            clauses, values = list(common), list(common_values)
            if item_id:
                if not item_id.startswith(prefix):
                    continue
                clauses.append(f"{id_column}=?")
                values.append(item_id[len(prefix) :])
            if query:
                escaped = query.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
                if prefix == "turn:":
                    clauses.append(
                        "(LOWER(prompt) LIKE ? ESCAPE '!' OR LOWER(reply) LIKE ? ESCAPE '!')"
                    )
                    values.extend((f"%{escaped}%", f"%{escaped}%"))
                else:
                    clauses.append("LOWER(text) LIKE ? ESCAPE '!'")
                    values.append(f"%{escaped}%")
            for boundary, operator in ((before, "<"), (after, ">")):
                if boundary is None:
                    continue
                if boundary[1].startswith(prefix):
                    clauses.append(f"({time_column},{id_column}) {operator} (?,?)")
                    values.extend((boundary[0], boundary[1][len(prefix) :]))
                else:
                    include_equal = (
                        prefix < boundary[1] if operator == "<" else prefix > boundary[1]
                    )
                    clauses.append(f"{time_column}{operator}{'=' if include_equal else ''}?")
                    values.append(boundary[0])
            where = "".join(" AND " + clause for clause in clauses)
            rows = conn.execute(
                select
                + where
                + f" ORDER BY {time_column} {direction},{id_column} {direction} LIMIT ?",
                (owner_id, thread_id, *values, limit),
            ).fetchall()
            combined.extend(ResidentHistoryRow.from_row(row) for row in rows)
        combined.sort(key=lambda row: (row.created_at, row.item_id), reverse=not after)
        return combined[:limit]

    def location(
        self, owner_id: int, thread_id: str, item_id: str, before: int, after: int
    ) -> tuple[int, list[ResidentHistoryRow], bool, bool]:
        with self._db.read_snapshot() as conn:
            revision = self._revision(conn, owner_id, thread_id)
            anchor = self._page(conn, owner_id, thread_id, 1, item_id=item_id)
            if not anchor:
                return revision, [], False, False
            point = (anchor[0].created_at, anchor[0].item_id)
            earlier = self._page(conn, owner_id, thread_id, before + 1, before=point)
            later = self._page(conn, owner_id, thread_id, after + 1, after=point)
            return (
                revision,
                [*reversed(earlier[:before]), anchor[0], *later[:after]],
                len(earlier) > before,
                len(later) > after,
            )
