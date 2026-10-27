"""Task identities and drafts for the local CLI transport."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import source_routes, tasks
from octop.infra.projects.conversation_migrations import legacy_link
from octop.infra.projects.ingress import SourceEnvelope
from octop.infra.utils.ulid import new_ulid


def preferences(services: SharedServices, owner_id: int, session_key: str) -> dict[str, Any]:
    key = hashlib.sha256(session_key.encode()).hexdigest()
    value = services.settings_repo.get(f"cli_task_session:{owner_id}:{key}")
    return json.loads(value) if value else {}


def save_preferences(
    services: SharedServices, owner_id: int, session_key: str, values: dict[str, Any]
) -> None:
    key = hashlib.sha256(session_key.encode()).hexdigest()
    session = f"cli_task_session:{owner_id}:{key}"
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        row = conn.execute("SELECT value FROM settings WHERE key=?", (session,)).fetchone()
        current = json.loads(row["value"]) if row else {}
        current.update({name: value for name, value in values.items() if name != "generation"})
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (session, json.dumps(current)),
        )


def draft(services: SharedServices, owner_id: int, session_key: str, request_id: str) -> None:
    key = hashlib.sha256(session_key.encode()).hexdigest()
    receipt = hashlib.sha256((session_key + ":" + request_id).encode()).hexdigest()
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        reset_key = f"cli_task_reset:{owner_id}:{receipt}"
        if conn.execute("SELECT value FROM settings WHERE key=?", (reset_key,)).fetchone():
            return
        session = f"cli_task_session:{owner_id}:{key}"
        row = conn.execute("SELECT value FROM settings WHERE key=?", (session,)).fetchone()
        values = json.loads(row["value"]) if row else {}
        values["generation"] = new_ulid()
        for name, value in ((session, json.dumps(values)), (reset_key, values["generation"])):
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (name, value),
            )


def envelope(
    services: SharedServices,
    owner_id: int,
    *,
    session_key: str,
    request_id: str,
    text: str,
    task_id: str | None = None,
    thread_id: str | None = None,
    agent_id: str | None = None,
    intent: dict[str, Any] | None = None,
) -> SourceEnvelope:
    if task_id and thread_id:
        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
    digest = hashlib.sha256(
        json.dumps({"text": text, "intent": intent or {}}, sort_keys=True).encode()
    ).hexdigest()
    legacy_agent_id = None
    if thread_id:
        thread = services.thread_repo.get(thread_id)
        if thread is None or thread.user_id != owner_id:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if services.thread_repo.is_resident(thread_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        legacy_agent_id = thread.agent_id
    key = f"task:{task_id}" if task_id else ""
    if task_id is None:
        # A request retry keeps its original source generation even after /new.
        # Persist the address before admission; the inbox still owns the result.
        request_key = (
            "cli_task_request_route:"
            + str(owner_id)
            + ":"
            + hashlib.sha256(json.dumps([session_key, request_id]).encode()).hexdigest()
        )
        session = (
            "cli_task_session:"
            + str(owner_id)
            + ":"
            + hashlib.sha256(session_key.encode()).hexdigest()
        )
        with services.db.transaction() as conn:
            if not services.project_task_repo.lock_owner(conn, owner_id):
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            row = conn.execute("SELECT value FROM settings WHERE key=?", (request_key,)).fetchone()
            if row:
                key = str(row["value"])
            else:
                row = conn.execute("SELECT value FROM settings WHERE key=?", (session,)).fetchone()
                saved = json.loads(row["value"]) if row else {}
                key = f"session:{session_key}:{saved.get('generation', '')}"
                conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (request_key, key))
    source = SourceEnvelope(
        source_kind="cli",
        source_instance=source_routes.local_instance(services),
        source_key=key,
        event_id=request_id,
        content_digest=digest,
        legacy_thread_id=thread_id,
        legacy_agent_id=legacy_agent_id,
    )
    if task_id:
        bind_task(services, owner_id, source, task_id)
    return source


def bind_task(
    services: SharedServices, owner_id: int, source: SourceEnvelope, task_id: str
) -> None:
    route = source_routes.resolve(
        services,
        owner_id,
        source_kind=source.source_kind,
        source_instance=source.source_instance,
        source_key=source.source_key,
    )
    source_routes.bind_task(services, owner_id, route.route_id, task_id)


def resolve_task(services: SharedServices, owner_id: int, identifier: str) -> str:
    if services.project_task_repo.get(identifier, owner_id):
        return identifier
    link = legacy_link(services, owner_id, identifier)
    if link.task_id:
        return link.task_id
    raise OctopError.localized(ErrorCode.TASK_BUSY)


def configure(
    services: SharedServices,
    owner_id: int,
    task_id: str,
    *,
    expert_id: str | None = None,
    change_expert: bool = False,
    mode: str | None = None,
    project_id: str | None = None,
    engine: tasks.EngineSelection | None = None,
) -> None:
    task = tasks.detail(services, owner_id, task_id)
    if project_id is not None and task.project_id != project_id:
        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
    changes: dict[str, Any] = {}
    if change_expert and task.selected_expert_id != expert_id:
        changes["selected_expert_id"] = expert_id
    if mode is not None and task.action_mode != mode:
        changes["action_mode"] = mode
    if engine is not None and task.engine != engine:
        changes["engine"] = engine
    if changes:
        tasks.patch(
            services,
            owner_id,
            task_id,
            tasks.TaskPatch(
                expected_revision=task.list_revision,
                expected_configuration_revision=task.configuration_revision,
                **changes,
            ),
        )


def listing(
    services: SharedServices,
    owner_id: int,
    *,
    limit: int = 50,
    expert_id: str | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    from octop.infra.projects.approval_policy import in_transaction

    with services.db.read_snapshot() as conn:
        fields: dict[str, Any] = {"selected_expert_id": expert_id} if expert_id else {}
        rows = services.project_task_repo.page(
            conn,
            owner_id,
            project_id,
            max(1, min(limit, 100)),
            scope=None if project_id else "all",
            **fields,
        )
        policy = in_transaction(conn, services, owner_id)
    return [tasks.summary(row, policy).model_dump(mode="json") for row in rows]
