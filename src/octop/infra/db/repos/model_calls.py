"""SQL-only invocation receipts, independent of the legacy usage aggregates."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int

ModelCallSource = Literal["box", "third_party"]
ModelCallStatus = Literal["running", "succeeded", "failed", "cancelled", "interrupted"]
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "uncached_input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
_TRACE_FIELDS = ("trace_id", "run_id", "parent_run_id", "request_id", "provider_request_id")
_USAGE_SOURCES = {
    "usage_metadata",
    "response_metadata",
    "llm_output",
    "mixed",
    "missing",
    "derived",
}
_IDENTITY_FIELDS = (
    "call_id",
    "parent_call_id",
    "submission_id",
    "user_id",
    "agent_id",
    "thread_id",
    "project_id",
    "task_id",
    "turn_id",
    "provider",
    "model_ref",
    "source",
    "purpose",
)
_COLUMNS = (*_IDENTITY_FIELDS, "started_at", "finished_at", "status", "usage_json", "metadata_json")


def _object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("Model call receipt must be a JSON object")
    return parsed


def _usage_json(value: str) -> str:
    raw = _object(value)
    counts = {
        key: raw[key] if type(raw.get(key)) is int and raw[key] >= 0 else None
        for key in TOKEN_FIELDS
    }
    presence = raw.get("presence")
    return json.dumps(
        {
            **counts,
            "presence": {
                key: counts[key] is not None
                and (not isinstance(presence, dict) or presence.get(key, True) is True)
                for key in TOKEN_FIELDS
            },
        },
        separators=(",", ":"),
    )


def _metadata_json(value: str) -> str:
    raw = _object(value)
    safe: dict[str, Any] = {}
    if type(raw.get("version")) is int and raw["version"] >= 0:
        safe["version"] = raw["version"]
    for key in _TRACE_FIELDS:
        token = raw.get(key)
        if isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", token):
            safe[key] = token
    if isinstance(raw.get("usage_source"), str) and raw["usage_source"] in _USAGE_SOURCES:
        safe["usage_source"] = raw["usage_source"]
    return json.dumps(safe, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class ModelCallRow:
    call_id: str
    started_at: int
    provider: str = ""
    model_ref: str = ""
    source: ModelCallSource = "third_party"
    purpose: str = "unknown"
    parent_call_id: str = ""
    submission_id: str = ""
    user_id: int | None = None
    agent_id: str = ""
    thread_id: str = ""
    project_id: str = ""
    task_id: str = ""
    turn_id: str = ""
    finished_at: int | None = None
    status: ModelCallStatus = "running"
    usage_json: str = "{}"
    metadata_json: str = "{}"
    pk: int = 0

    @classmethod
    def from_row(cls, row: DbRow) -> ModelCallRow:
        return cls(
            call_id=str(row["call_id"]),
            parent_call_id=str(row["parent_call_id"]),
            submission_id=str(row["submission_id"]),
            agent_id=str(row["agent_id"]),
            thread_id=str(row["thread_id"]),
            project_id=str(row["project_id"]),
            task_id=str(row["task_id"]),
            turn_id=str(row["turn_id"]),
            provider=str(row["provider"]),
            model_ref=str(row["model_ref"]),
            purpose=str(row["purpose"]),
            source=cast(ModelCallSource, str(row["source"])),
            user_id=strict_int(row["user_id"]) if row["user_id"] is not None else None,
            started_at=strict_int(row["started_at"]),
            finished_at=strict_int(row["finished_at"]) if row["finished_at"] is not None else None,
            status=cast(ModelCallStatus, str(row["status"])),
            usage_json=str(row["usage_json"]),
            metadata_json=str(row["metadata_json"]),
            pk=strict_int(row["id"]),
        )


@dataclass(frozen=True)
class AttributedTaskModelCall:
    row: ModelCallRow
    task_id: str
    task_title: str
    project_id: str
    project_name: str
    attribution: Literal["direct", "submission", "unassigned"]


def _validated(row: ModelCallRow) -> ModelCallRow:
    if not row.call_id or len(row.call_id) > 256:
        raise ValueError("Invalid model call id")
    if row.user_id is not None and (type(row.user_id) is not int or row.user_id < 1):
        raise ValueError("Invalid model call user")
    if type(row.started_at) is not int or row.started_at < 0:
        raise ValueError("Invalid model call start time")
    if row.finished_at is not None and (
        type(row.finished_at) is not int or row.finished_at < row.started_at
    ):
        raise ValueError("Invalid model call finish time")
    if row.source not in {"box", "third_party"}:
        raise ValueError("Invalid model call source")
    if row.status not in {"running", "succeeded", "failed", "cancelled", "interrupted"}:
        raise ValueError("Invalid model call status")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", row.purpose):
        raise ValueError("Invalid model call purpose")
    return replace(
        row, usage_json=_usage_json(row.usage_json), metadata_json=_metadata_json(row.metadata_json)
    )


class ModelCallRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def _upsert(self, conn: Any, row: ModelCallRow) -> ModelCallRow:
        conn.execute(
            f"INSERT INTO model_calls({','.join(_COLUMNS)}) VALUES ({','.join('?' for _ in _COLUMNS)}) "
            "ON CONFLICT(call_id) DO NOTHING",
            tuple(getattr(row, key) for key in _COLUMNS),
        )
        lock = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
        saved = conn.execute(
            "SELECT * FROM model_calls WHERE call_id=?" + lock, (row.call_id,)
        ).fetchone()
        assert saved is not None
        current = ModelCallRow.from_row(saved)
        if any(getattr(current, key) != getattr(row, key) for key in _IDENTITY_FIELDS):
            raise ValueError("Model call identity changed")
        return current

    def start(self, row: ModelCallRow) -> ModelCallRow:
        """Keep the first start and immutable identity when an observer replays."""
        row = _validated(row)
        if row.status != "running" or row.finished_at is not None:
            raise ValueError("Model call start must be running")
        with self._db.transaction() as conn:
            return self._upsert(conn, row)

    def finish(self, row: ModelCallRow) -> ModelCallRow:
        """Upsert one terminal receipt; late receipts only fill unknown usage."""
        row = _validated(row)
        if row.status == "running" or row.finished_at is None:
            raise ValueError("Model call finish must be terminal")
        with self._db.transaction() as conn:
            current = self._upsert(conn, row)
            if row.finished_at < current.started_at:
                raise ValueError("Model call finish precedes its original start")
            old_usage, incoming = _object(current.usage_json), _object(row.usage_json)
            receipts = {
                key: old_usage
                if old_usage.get(key) is not None
                and (
                    old_usage["presence"][key]
                    or not incoming["presence"][key]
                    or old_usage[key] != incoming.get(key)
                )
                else incoming
                for key in TOKEN_FIELDS
            }
            merged = {key: receipts[key].get(key) for key in TOKEN_FIELDS}
            merged["presence"] = {key: receipts[key]["presence"][key] for key in TOKEN_FIELDS}
            terminal = current if current.status != "running" else row
            metadata = {**_object(current.metadata_json), **_object(row.metadata_json)}
            old_source = _object(current.metadata_json).get("usage_source")
            if old_source not in {None, "missing", "derived"} and metadata.get("usage_source") in {
                "missing",
                "derived",
            }:
                metadata["usage_source"] = old_source
            conn.execute(
                "UPDATE model_calls SET status=?,finished_at=?,usage_json=?,metadata_json=? WHERE call_id=?",
                (
                    terminal.status,
                    terminal.finished_at,
                    _usage_json(json.dumps(merged)),
                    _metadata_json(json.dumps(metadata)),
                    row.call_id,
                ),
            )
            saved = conn.execute(
                "SELECT * FROM model_calls WHERE call_id=?", (row.call_id,)
            ).fetchone()
            assert saved is not None
            return ModelCallRow.from_row(saved)

    def get(self, call_id: str, *, user_id: int | None = None) -> ModelCallRow | None:
        where = "call_id=?"
        values: list[Any] = [call_id]
        if user_id is not None:
            where += " AND user_id=?"
            values.append(user_id)
        with self._db.connect() as conn:
            row = conn.execute(f"SELECT * FROM model_calls WHERE {where}", values).fetchone()
        return ModelCallRow.from_row(row) if row is not None else None

    def list_task_usage(
        self,
        *,
        user_id: int | None,
        range_start: int,
        range_end: int,
        task_id: str | None = None,
        project_id: str | None = None,
    ) -> list[AttributedTaskModelCall]:
        """Read task usage calls by start time in the millisecond [start, end) range.

        Direct task IDs survive resource deletion. Calls without one can use an
        accepted submission only when it identifies one task for the same user.
        Project snapshots win; absent snapshots fall back to the current task.
        Without resource filters, unmatched calls remain explicitly unassigned.
        Callers enforce whether an unscoped user read is allowed.
        """
        call_scope = ["c.started_at>=?", "c.started_at<?"]
        params: list[Any] = [range_start, range_end]
        if user_id is not None:
            call_scope.append("c.user_id=?")
            params.append(user_id)
        resource_scope: list[str] = []
        if task_id is not None or project_id is not None:
            resource_scope.append("c.effective_task_id<>''")
        for column, value in (("effective_task_id", task_id), ("effective_project_id", project_id)):
            if value is not None:
                resource_scope.append(f"c.{column}=?")
                params.append(value)
        resource_clause = " AND ".join(resource_scope) if resource_scope else "1=1"
        with self._db.connect() as conn:
            rows = conn.execute(
                f"""
                WITH accepted_submissions AS (
                    SELECT t.owner_id, r.request_id, MIN(t.task_id) AS task_id
                    FROM project_task_turns r
                    JOIN project_tasks t ON t.task_id=r.task_id
                    WHERE r.request_id<>''
                    GROUP BY t.owner_id, r.request_id
                    HAVING COUNT(DISTINCT t.task_id)=1
                ), attributed AS (
                    SELECT c.*,
                        COALESCE(NULLIF(c.task_id, ''), s.task_id, '') AS effective_task_id,
                        CASE WHEN c.task_id<>'' THEN 'direct'
                            WHEN s.task_id IS NOT NULL THEN 'submission'
                            ELSE 'unassigned' END AS attribution
                    FROM model_calls c
                    LEFT JOIN accepted_submissions s ON c.task_id=''
                        AND c.user_id=s.owner_id AND c.submission_id=s.request_id
                    WHERE {" AND ".join(call_scope)}
                ), task_calls AS (
                    SELECT c.*, COALESCE(t.title, '') AS task_title,
                        COALESCE(NULLIF(c.project_id, ''), t.project_id, '') AS effective_project_id
                    FROM attributed c
                    LEFT JOIN project_tasks t ON t.task_id=c.effective_task_id
                        AND t.owner_id=c.user_id
                )
                SELECT c.*, COALESCE(p.name, '') AS project_name
                FROM task_calls c
                LEFT JOIN projects p ON p.project_id=c.effective_project_id AND p.owner_id=c.user_id
                    AND c.effective_task_id<>''
                WHERE {resource_clause}
                ORDER BY c.started_at, c.id
                """,
                params,
            ).fetchall()
        return [
            AttributedTaskModelCall(
                row=ModelCallRow.from_row(row),
                task_id=str(row["effective_task_id"]),
                task_title=str(row["task_title"]),
                project_id=str(row["effective_project_id"]),
                project_name=str(row["project_name"]),
                attribution=cast(Literal["direct", "submission", "unassigned"], row["attribution"]),
            )
            for row in rows
        ]

    def list_page(
        self,
        *,
        user_id: int | None = None,
        agent_id: str | None = None,
        thread_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        turn_id: str | None = None,
        submission_id: str | None = None,
        status: ModelCallStatus | None = None,
        purpose: str | None = None,
        source: ModelCallSource | None = None,
        model_ref: str | None = None,
        provider: str | None = None,
        before_id: int | None = None,
        limit: int = 100,
    ) -> list[ModelCallRow]:
        """Read newest receipts; callers enforce whether an unscoped read is allowed."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Invalid model call page size")
        where: list[str] = []
        params: list[Any] = []
        for key, value in (
            ("user_id", user_id),
            ("agent_id", agent_id),
            ("thread_id", thread_id),
            ("project_id", project_id),
            ("task_id", task_id),
            ("turn_id", turn_id),
            ("submission_id", submission_id),
            ("status", status),
            ("purpose", purpose),
            ("source", source),
            ("model_ref", model_ref),
            ("provider", provider),
        ):
            if value is not None:
                where.append(f"{key}=?")
                params.append(value)
        if before_id is not None:
            where.append("id<?")
            params.append(before_id)
        params.append(limit)
        clause = " AND ".join(where) if where else "1=1"
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM model_calls WHERE {clause} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [ModelCallRow.from_row(row) for row in rows]
