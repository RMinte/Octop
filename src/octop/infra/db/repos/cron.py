"""Cron jobs table access."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from octop.infra.cron.task_type import (
    DEFAULT_CRON_TASK_TYPE,
    CronTaskType,
    default_cron_name,
    normalize_cron_task_type,
    require_cron_task_type,
)
from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import (
    UNSET,
    DbRow,
    bool_int,
    map_rows,
    now_ts,
    partial_updates,
)

__all__ = [
    "DEFAULT_CRON_TASK_TYPE",
    "CronJobRepo",
    "CronJobRow",
    "CronTaskType",
    "normalize_cron_task_type",
    "require_cron_task_type",
]


def _encode_mcp_servers(names: list[str] | None) -> str:
    cleaned = [str(n).strip() for n in (names or []) if str(n).strip()]
    return json.dumps(cleaned, ensure_ascii=False)


def _decode_mcp_servers(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(n).strip() for n in raw if str(n).strip()]
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(n).strip() for n in parsed if str(n).strip()]


def _optional_str(r: DbRow, key: str) -> str | None:
    try:
        value = r[key]
    except (KeyError, IndexError):
        return None
    text = str(value).strip() if value else ""
    return text or None


@dataclass(frozen=True)
class CronJobRow:
    id: int
    cron_id: str
    name: str
    agent_id: str | None
    user_id: int
    trigger: str
    prompt: str
    session_key: str
    model: str | None
    fresh_thread: int
    enabled: int
    task_type: str
    mcp_servers: list[str]
    last_run_at: int | None
    last_status: str | None
    last_error: str | None
    created_at: int
    updated_at: int
    conversation_policy: str = "continue_target"
    target_task_id: str | None = None
    project_id: str | None = None
    selected_expert_id: str | None = None

    @classmethod
    def from_row(cls, r: DbRow) -> CronJobRow:
        return cls(
            id=r["id"],
            cron_id=r["cron_id"],
            name=_optional_str(r, "name") or default_cron_name(r["prompt"], r["cron_id"]),
            agent_id=r["agent_id"],
            user_id=r["user_id"],
            trigger=r["schedule_spec"],
            prompt=r["prompt"],
            session_key=r["session_key"],
            model=r["model"],
            fresh_thread=r["fresh_thread"],
            enabled=r["enabled"],
            task_type=normalize_cron_task_type(str(r["task_type"])),
            mcp_servers=_decode_mcp_servers(r["mcp_servers"]),
            last_run_at=r["last_run_at"],
            last_status=r["last_status"],
            last_error=r["last_error"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            conversation_policy=str(r["conversation_policy"]),
            target_task_id=_optional_str(r, "target_task_id"),
            project_id=_optional_str(r, "project_id"),
            selected_expert_id=_optional_str(r, "selected_expert_id"),
        )

    def to_public_dict(self, *, include_agent: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.cron_id,
            "name": self.name,
            "trigger": self.trigger,
            "prompt": self.prompt,
            "session_key": self.session_key,
            "model": self.model,
            "fresh_thread": bool(self.fresh_thread),
            "enabled": bool(self.enabled),
            "task_type": self.task_type,
            "mcp_servers": list(self.mcp_servers),
            "last_run_at": self.last_run_at,
            "last_status": self.last_status,
            "conversation_policy": self.conversation_policy,
            "target_task_id": self.target_task_id,
            "project_id": self.project_id,
            "selected_expert_id": self.selected_expert_id,
        }
        if include_agent:
            out["agent_id"] = self.agent_id
            out["last_error"] = self.last_error
        return out


class CronJobRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        cron_id: str,
        agent_id: str | None,
        user_id: int,
        trigger: str,
        prompt: str,
        session_key: str,
        fresh_thread: bool = False,
        model: str | None = None,
        task_type: CronTaskType = DEFAULT_CRON_TASK_TYPE,
        mcp_servers: list[str] | None = None,
        enabled: bool = True,
        name: str | None = None,
        conversation_policy: str = "continue_target",
        target_task_id: str | None = None,
        project_id: str | None = None,
        selected_expert_id: str | None | object = UNSET,
    ) -> str:
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO cron_jobs(cron_id, name, agent_id, user_id, schedule_spec, prompt, "
                "session_key, model, fresh_thread, task_type, mcp_servers, enabled, "
                "created_at, updated_at,conversation_policy,target_task_id,project_id,selected_expert_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,?,?,?,?)",
                (
                    cron_id,
                    (name or "").strip() or default_cron_name(prompt, cron_id),
                    agent_id,
                    user_id,
                    trigger,
                    prompt,
                    session_key,
                    model,
                    bool_int(fresh_thread),
                    task_type,
                    _encode_mcp_servers(mcp_servers),
                    bool_int(enabled),
                    ts,
                    ts,
                    conversation_policy,
                    target_task_id,
                    project_id,
                    agent_id if selected_expert_id is UNSET else selected_expert_id,
                ),
            )
        return cron_id

    def get(self, cron_id: str) -> CronJobRow | None:
        with self._db.connect() as conn:
            r = conn.execute("SELECT * FROM cron_jobs WHERE cron_id = ?", (cron_id,)).fetchone()
        return CronJobRow.from_row(r) if r else None

    def list_by_agent(
        self,
        agent_id: str,
        *,
        include_disabled: bool = True,
        user_id: int | None = None,
    ) -> list[CronJobRow]:
        sql = "SELECT * FROM cron_jobs WHERE agent_id = ?"
        params: list[object] = [agent_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if not include_disabled:
            sql += " AND enabled = 1"
        sql += " ORDER BY created_at DESC"
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return map_rows(rows, CronJobRow)

    def list_all(self, *, include_disabled: bool = True) -> list[CronJobRow]:
        sql = "SELECT * FROM cron_jobs"
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY agent_id, created_at DESC"
        with self._db.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return map_rows(rows, CronJobRow)

    def set_run_status(
        self,
        cron_id: str,
        *,
        ts: int,
        status: str,
        error: str | None = None,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE cron_jobs SET last_run_at = ?, last_status = ?, last_error = ?, "
                "updated_at = ? WHERE cron_id = ?",
                (ts, status, error, now_ts(), cron_id),
            )

    def update(
        self,
        cron_id: str,
        *,
        trigger: str | None = None,
        name: str | None = None,
        prompt: str | None = None,
        session_key: str | None = None,
        fresh_thread: bool | None = None,
        enabled: bool | None = None,
        task_type: CronTaskType | None = None,
        model: str | None | object = UNSET,
        mcp_servers: list[str] | None | object = UNSET,
        conversation_policy: str | None = None,
        target_task_id: str | None | object = UNSET,
        project_id: str | None | object = UNSET,
        selected_expert_id: str | None | object = UNSET,
    ) -> None:
        fields, params = partial_updates(
            [
                ("schedule_spec", trigger),
                ("name", name),
                ("prompt", prompt),
                ("session_key", session_key),
                ("task_type", task_type),
                ("conversation_policy", conversation_policy),
            ]
        )
        if fresh_thread is not None:
            fields.append("fresh_thread = ?")
            params.append(bool_int(fresh_thread))
        for key, value in (
            ("target_task_id", target_task_id),
            ("project_id", project_id),
            ("selected_expert_id", selected_expert_id),
        ):
            if value is not UNSET:
                fields.append(key + " = ?")
                params.append(value)
        if model is not UNSET:
            fields.append("model = ?")
            params.append(model)
        if mcp_servers is not UNSET:
            fields.append("mcp_servers = ?")
            params.append(
                _encode_mcp_servers(mcp_servers if isinstance(mcp_servers, list) else None)
            )
        if enabled is not None:
            fields.append("enabled = ?")
            params.append(bool_int(enabled))
        if not fields:
            return
        fields.append("updated_at = ?")
        params.append(now_ts())
        params.append(cron_id)
        with self._db.transaction() as conn:
            changed = conn.execute(
                f"UPDATE cron_jobs SET {', '.join(fields)} WHERE cron_id = ?", params
            ).rowcount
            if changed and (project_id is not UNSET or target_task_id is not UNSET):
                conn.execute(
                    "DELETE FROM settings WHERE key=?", (f"deleted_cron_binding:{cron_id}",)
                )

    def delete(self, cron_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM cron_jobs WHERE cron_id = ?", (cron_id,))
            conn.execute("DELETE FROM settings WHERE key=?", (f"deleted_cron_binding:{cron_id}",))
