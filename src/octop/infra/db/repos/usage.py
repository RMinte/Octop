"""Token usage ledger access."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import (
    DbRow,
    insert_returning_id,
    now_ts,
)

_DAY_S = 86_400
_DAY_WINDOW_RE = re.compile(r"^day:(\d{4}-\d{2}-\d{2})$")
_MONTH_WINDOW_RE = re.compile(r"^month:(\d{4}-\d{2})$")
_RANGE_WINDOW_RE = re.compile(r"^range:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})$")
DETAIL_EXPORT_LIMIT = 50_000

# Task journals store millisecond timestamps and only input/output totals.
# Keep unknown breakdowns at zero rather than inferring calls or cache usage.
# Title jobs have no usage timestamp; their usage belongs to the first turn,
# whose model is also the one the naming worker uses.
_USAGE_SQL = """
    SELECT id, ts, agent_id, user_id, thread_id, model,
        input_tokens, uncached_input_tokens, cache_read_tokens, cache_write_tokens,
        output_tokens, reasoning_tokens, total_tokens, model_calls, source,
        '' AS project_id, '' AS task_id, '' AS project_name, '' AS task_title, 1 AS usage_turns
    FROM usage_log
    UNION ALL
    SELECT r.id, CAST(r.created_at / 1000 AS BIGINT), '', t.owner_id, t.task_id, r.model_ref,
        r.input_tokens, 0, 0, 0, r.output_tokens, 0,
        r.input_tokens + r.output_tokens, 0, 'project_task',
        COALESCE(t.project_id, ''), t.task_id, COALESCE(p.name, ''), t.title, 1
    FROM project_task_turns r
    JOIN project_tasks t ON t.task_id = r.task_id
    LEFT JOIN projects p ON p.project_id = t.project_id AND p.owner_id = t.owner_id
    WHERE r.input_tokens <> 0 OR r.output_tokens <> 0
    UNION ALL
    SELECT r.id, CAST(r.created_at / 1000 AS BIGINT), '', t.owner_id, t.task_id, r.model_ref,
        j.input_tokens, 0, 0, 0, j.output_tokens, 0,
        j.input_tokens + j.output_tokens, 0, 'project_task_title',
        COALESCE(t.project_id, ''), t.task_id, COALESCE(p.name, ''), t.title, 0
    FROM project_task_titles j
    JOIN project_tasks t ON t.task_id = j.task_id
    JOIN project_task_turns r ON r.task_id = t.task_id AND r.request_id = t.request_id
    LEFT JOIN projects p ON p.project_id = t.project_id AND p.owner_id = t.owner_id
    WHERE j.input_tokens <> 0 OR j.output_tokens <> 0
    UNION ALL
    SELECT j.id, CAST(j.created_at / 1000 AS BIGINT), '', t.owner_id, t.task_id, j.model_ref,
        j.input_tokens, 0, 0, 0, j.output_tokens, 0,
        j.input_tokens + j.output_tokens, 0, 'project_task_review',
        COALESCE(t.project_id, ''), t.task_id, COALESCE(p.name, ''), t.title, 0
    FROM project_task_reviews j
    JOIN project_tasks t ON t.task_id = j.task_id AND t.owner_id = j.owner_id
    LEFT JOIN projects p ON p.project_id = t.project_id AND p.owner_id = t.owner_id
    WHERE j.input_tokens <> 0 OR j.output_tokens <> 0
"""
_TOKEN_FIELDS = (
    "input_tokens",
    "uncached_input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "model_calls",
)
_SUM_SQL = ", ".join(f"COALESCE(SUM({field}), 0) AS {field}" for field in _TOKEN_FIELDS)


@dataclass(frozen=True)
class UsageRow:
    id: int
    ts: int
    agent_id: str
    user_id: int
    thread_id: str
    model: str
    input_tokens: int
    uncached_input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    model_calls: int
    source: str
    project_id: str = ""
    task_id: str = ""
    project_name: str = ""
    task_title: str = ""

    @classmethod
    def from_row(cls, r: DbRow) -> UsageRow:
        return cls(
            id=r["id"],
            ts=r["ts"],
            agent_id=r["agent_id"],
            user_id=r["user_id"],
            thread_id=r["thread_id"],
            model=r["model"],
            input_tokens=r["input_tokens"],
            uncached_input_tokens=r["uncached_input_tokens"],
            cache_read_tokens=r["cache_read_tokens"],
            cache_write_tokens=r["cache_write_tokens"],
            output_tokens=r["output_tokens"],
            reasoning_tokens=r["reasoning_tokens"],
            total_tokens=r["total_tokens"],
            model_calls=r["model_calls"],
            source=r["source"],
            project_id=r["project_id"],
            task_id=r["task_id"],
            project_name=r["project_name"],
            task_title=r["task_title"],
        )


def _zoneinfo(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def resolve_usage_window(
    window: str,
    *,
    timezone: str = "UTC",
    now: int | None = None,
) -> tuple[int, int]:
    """Map a window alias to ``[start, end)`` unix seconds in *timezone*.

    Supported:
      today | yesterday | last_7d | last_30d | all
      day:YYYY-MM-DD | month:YYYY-MM
      range:YYYY-MM-DD:YYYY-MM-DD  (inclusive calendar days)
    """
    tz = _zoneinfo(timezone)
    now_ts_val = int(time.time() if now is None else now)
    now_dt = datetime.fromtimestamp(now_ts_val, tz=tz)
    end_open = now_ts_val + 1

    day_match = _DAY_WINDOW_RE.fullmatch(window)
    if day_match is not None:
        day = datetime.strptime(day_match.group(1), "%Y-%m-%d").date()
        start_dt = datetime(day.year, day.month, day.day, tzinfo=tz)
        end_dt = start_dt + timedelta(days=1)
        return int(start_dt.timestamp()), int(end_dt.timestamp())

    month_match = _MONTH_WINDOW_RE.fullmatch(window)
    if month_match is not None:
        year_s, month_s = month_match.group(1).split("-")
        year, month = int(year_s), int(month_s)
        start_dt = datetime(year, month, 1, tzinfo=tz)
        if month == 12:
            end_dt = datetime(year + 1, 1, 1, tzinfo=tz)
        else:
            end_dt = datetime(year, month + 1, 1, tzinfo=tz)
        return int(start_dt.timestamp()), int(end_dt.timestamp())

    range_match = _RANGE_WINDOW_RE.fullmatch(window)
    if range_match is not None:
        start_day = datetime.strptime(range_match.group(1), "%Y-%m-%d").date()
        end_day = datetime.strptime(range_match.group(2), "%Y-%m-%d").date()
        if end_day < start_day:
            raise ValueError(f"invalid usage window: {window!r}")
        start_dt = datetime(start_day.year, start_day.month, start_day.day, tzinfo=tz)
        end_dt = datetime(end_day.year, end_day.month, end_day.day, tzinfo=tz) + timedelta(days=1)
        return int(start_dt.timestamp()), int(end_dt.timestamp())

    if window.startswith("day:") or window.startswith("month:") or window.startswith("range:"):
        raise ValueError(f"invalid usage window: {window!r}")

    if window == "today":
        start_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start_dt.timestamp()), end_open
    if window == "yesterday":
        today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = today_start - timedelta(days=1)
        return int(start_dt.timestamp()), int(today_start.timestamp())
    if window == "last_7d":
        return now_ts_val - 7 * _DAY_S, end_open
    if window == "last_30d":
        return now_ts_val - 30 * _DAY_S, end_open
    if window == "all":
        return 0, end_open
    # Default: last_30d
    return now_ts_val - 30 * _DAY_S, end_open


class UsageRepo:
    def __init__(self, db: DatabasePool):
        self._db = db

    # --- write -----------------------------------------------------------

    def record(
        self,
        *,
        agent_id: str,
        user_id: int,
        input_tokens: int,
        output_tokens: int,
        uncached_input_tokens: int | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        model_calls: int = 1,
        model: str = "",
        thread_id: str = "",
        source: str = "chat",
        ts: int | None = None,
    ) -> int:
        """Append one usage row. Returns the new row id."""
        with self._db.connect() as conn:
            return insert_returning_id(
                conn,
                """
                INSERT INTO usage_log (
                    ts, agent_id, user_id, thread_id, model,
                    input_tokens, uncached_input_tokens,
                    cache_read_tokens, cache_write_tokens,
                    output_tokens, reasoning_tokens, total_tokens, model_calls, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts or now_ts(),
                    agent_id,
                    user_id,
                    thread_id,
                    model,
                    int(input_tokens),
                    int(input_tokens)
                    if uncached_input_tokens is None
                    else int(uncached_input_tokens),
                    int(cache_read_tokens),
                    int(cache_write_tokens),
                    int(output_tokens),
                    int(reasoning_tokens),
                    int(input_tokens) + int(output_tokens),
                    int(model_calls),
                    source,
                ),
            )

    # --- read / aggregate ------------------------------------------------

    @staticmethod
    def _day_buckets(
        conn: Any, where_sql: str, params: list[Any], timezone: str
    ) -> list[dict[str, Any]]:
        """Fold timestamps into server calendar days, including DST boundaries."""
        zone = _zoneinfo(timezone)
        buckets: dict[str, dict[str, Any]] = {}
        rows = conn.execute(
            f"""
            SELECT ts, {_SUM_SQL}, COALESCE(SUM(usage_turns), 0) AS turns
            FROM ({_USAGE_SQL}) AS usage WHERE {where_sql}
            GROUP BY ts ORDER BY ts DESC
            """,
            params,
        )
        for row in rows:
            day = datetime.fromtimestamp(row["ts"], tz=zone).date().isoformat()
            if day not in buckets:
                if len(buckets) == 100:
                    break
                buckets[day] = {
                    "key": day,
                    "label": day,
                    **dict.fromkeys((*_TOKEN_FIELDS, "turns"), 0),
                }
            for field in (*_TOKEN_FIELDS, "turns"):
                buckets[day][field] += int(row[field])
        return list(buckets.values())

    def _scope_filter(
        self,
        *,
        user_id: int | None,
        agent_id: str | None,
        project_id: str | None,
        task_id: str | None,
        window: str,
        timezone: str,
    ) -> tuple[str, list[Any], int, int]:
        start, end = resolve_usage_window(window, timezone=timezone)
        where: list[str] = ["ts >= ?", "ts < ?"]
        params: list[Any] = [start, end]
        for column, value in (
            ("user_id", user_id),
            ("agent_id", agent_id),
            ("project_id", project_id),
            ("task_id", task_id),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        if project_id is not None or task_id is not None:
            where.append("task_id <> ''")
        return " AND ".join(where), params, start, end

    def summary(
        self,
        *,
        user_id: int | None = None,
        agent_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        window: str = "last_30d",
        granularity: str = "by_day",
        timezone: str = "UTC",
    ) -> dict[str, Any]:
        """Aggregate expert and independent task usage within the requested scope.

        Task/project views include task consumption only; the agent view includes
        expert sessions only. Title and review costs contribute tokens, not turns.
        """
        where_sql, params, start, end = self._scope_filter(
            user_id=user_id,
            agent_id=agent_id,
            project_id=project_id,
            task_id=task_id,
            window=window,
            timezone=timezone,
        )
        if granularity in {"by_project", "by_task"}:
            where_sql += " AND task_id <> ''"
        elif granularity == "by_agent":
            where_sql += " AND task_id = ''"

        sum_sql = _SUM_SQL + ", COALESCE(SUM(usage_turns), 0) AS turns"
        with self._db.connect() as conn:
            row = conn.execute(
                f"""
                SELECT {sum_sql},
                    COALESCE(SUM(CASE WHEN task_id = '' THEN input_tokens ELSE 0 END), 0)
                        AS cache_input_tokens
                FROM ({_USAGE_SQL}) AS usage WHERE {where_sql}
                """,
                params,
            ).fetchone()
            totals = {field: int(row[field]) for field in (*_TOKEN_FIELDS, "turns")}
            cache_input_tokens = int(row["cache_input_tokens"])

            bucket_exprs = {
                "by_agent": "agent_id",
                "by_model": "COALESCE(NULLIF(model, ''), '(unknown)')",
                "by_project": "project_id",
                "by_task": "task_id",
            }
            buckets: list[dict[str, Any]] = []
            if granularity == "by_day":
                buckets = self._day_buckets(conn, where_sql, params, timezone)
            elif bucket_expr := bucket_exprs.get(granularity):
                label_expr = {
                    "by_project": "MAX(project_name)",
                    "by_task": "MAX(task_title)",
                }.get(granularity, bucket_expr)
                bucket_rows = conn.execute(
                    f"""
                    SELECT {bucket_expr} AS bucket, {label_expr} AS label, {sum_sql}
                    FROM ({_USAGE_SQL}) AS usage WHERE {where_sql}
                    GROUP BY {bucket_expr}
                    ORDER BY total_tokens DESC, bucket
                    LIMIT 100
                    """,
                    params,
                ).fetchall()
                buckets = [
                    {
                        "key": r["bucket"],
                        "label": r["label"],
                        **{field: int(r[field]) for field in (*_TOKEN_FIELDS, "turns")},
                    }
                    for r in bucket_rows
                ]

        avg = totals["total_tokens"] // totals["turns"] if totals["turns"] else 0
        cache_hit_percent = (
            round(totals["cache_read_tokens"] / cache_input_tokens * 100)
            if cache_input_tokens
            else 0
        )
        return {
            "window": window,
            "granularity": granularity,
            "range_start": start,
            "range_end": end,
            **totals,
            "avg_per_turn": avg,
            "cache_hit_percent": cache_hit_percent,
            "buckets": buckets,
        }

    def list_detail(
        self,
        *,
        user_id: int | None = None,
        agent_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        window: str = "last_30d",
        timezone: str = "UTC",
        limit: int = DETAIL_EXPORT_LIMIT,
    ) -> list[UsageRow]:
        """Return expert and task usage for Excel export (oldest → newest).

        When ``limit`` truncates, keep the newest ``limit`` usage records.
        """
        where_sql, params, _start, _end = self._scope_filter(
            user_id=user_id,
            agent_id=agent_id,
            project_id=project_id,
            task_id=task_id,
            window=window,
            timezone=timezone,
        )
        cap = max(1, min(int(limit), DETAIL_EXPORT_LIMIT))
        with self._db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM ({_USAGE_SQL}) AS usage
                    WHERE {where_sql}
                    ORDER BY ts DESC, source DESC, id DESC
                    LIMIT ?
                ) AS recent
                ORDER BY ts ASC, source ASC, id ASC
                """,
                [*params, cap],
            ).fetchall()
        return [UsageRow.from_row(r) for r in rows]

    def thread_totals(self, *, agent_id: str, thread_id: str) -> dict[str, int]:
        """Aggregate token usage for a single thread."""
        with self._db.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                    COALESCE(SUM(uncached_input_tokens), 0) AS uncached_input_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                    COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                    COALESCE(SUM(total_tokens), 0)  AS total_tokens,
                    COALESCE(SUM(model_calls), 0) AS model_calls,
                    COUNT(*)                         AS turns
                FROM usage_log
                WHERE agent_id = ? AND thread_id = ?
                """,
                (agent_id, thread_id),
            ).fetchone()
        return {
            "input_tokens": int(row["input_tokens"]),
            "uncached_input_tokens": int(row["uncached_input_tokens"]),
            "cache_read_tokens": int(row["cache_read_tokens"]),
            "cache_write_tokens": int(row["cache_write_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "reasoning_tokens": int(row["reasoning_tokens"]),
            "total_tokens": int(row["total_tokens"]),
            "model_calls": int(row["model_calls"]),
            "turns": int(row["turns"]),
        }

    def last_thread_input_tokens(self, *, agent_id: str, thread_id: str) -> int:
        """Most recent turn's prompt tokens for *thread_id* (context-ring fallback)."""
        with self._db.connect() as conn:
            row = conn.execute(
                """
                SELECT input_tokens FROM usage_log
                WHERE agent_id = ? AND thread_id = ?
                ORDER BY ts DESC, id DESC
                LIMIT 1
                """,
                (agent_id, thread_id),
            ).fetchone()
        if row is None:
            return 0
        return int(row["input_tokens"] or 0)

    def total_tokens_for_user(self, user_id: int) -> int:
        """Lifetime ``total_tokens`` for one user (all agents and sources)."""
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT (SELECT COALESCE(SUM(total_tokens), 0) FROM usage_log WHERE user_id=?) "
                "+ (SELECT COALESCE(SUM(r.input_tokens + r.output_tokens), 0) "
                "FROM project_task_turns r JOIN project_tasks t ON t.task_id=r.task_id WHERE t.owner_id=?) "
                "+ (SELECT COALESCE(SUM(j.input_tokens + j.output_tokens), 0) "
                "FROM project_task_titles j JOIN project_tasks t ON t.task_id=j.task_id WHERE t.owner_id=?) "
                "+ (SELECT COALESCE(SUM(input_tokens + output_tokens), 0) "
                "FROM project_task_reviews WHERE owner_id=?) AS total",
                (user_id, user_id, user_id, user_id),
            ).fetchone()
        return int(row["total"] if row is not None else 0)
