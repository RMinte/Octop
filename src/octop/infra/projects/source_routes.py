"""Durable source heads and the legacy-writer/migration fence."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from octop.infra.agents.resident_worker_lock import ResidentWorkerLock
from octop.infra.db.repos.unified_sources import UnifiedSourceRoute, UnifiedSourceRouteRepo
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.deletion_state import assert_project_writable
from octop.infra.utils.ulid import new_ulid


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def local_instance(services: SharedServices) -> str:
    with services.db.transaction() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('unified_source_instance',?) ON CONFLICT(key) DO NOTHING",
            (new_ulid(),),
        )
        row = conn.execute(
            "SELECT value FROM settings WHERE key='unified_source_instance'"
        ).fetchone()
        assert row is not None
        return str(row["value"])


def require_writable(route: UnifiedSourceRoute, *, legacy: bool = False) -> None:
    if route.state not in ({"legacy"} if legacy else {"task", "draft"}):
        raise OctopError.localized(ErrorCode.TASK_BUSY, status=409)


@contextmanager
def writer_fence(services: SharedServices, thread_id: str) -> Iterator[None]:
    """Nonblocking cross-process exclusion; hold through legacy I/O or migration commit."""
    token = hashlib.sha256(thread_id.encode()).hexdigest()
    lock = ResidentWorkerLock(services.paths.root / "source-fences" / (token + ".lock"))
    try:
        lock.acquire()
    except RuntimeError as exc:
        raise OctopError.localized(ErrorCode.TASK_BUSY, status=409) from exc
    try:
        yield
    finally:
        lock.release()


def legacy_route(
    services: SharedServices, owner_id: int, thread_id: str, agent_id: str | None = None
) -> UnifiedSourceRoute:
    instance = local_instance(services)
    with services.db.transaction() as conn:
        return UnifiedSourceRouteRepo.ensure(
            conn,
            owner_id=owner_id,
            source_kind="legacy_thread",
            source_instance=instance,
            source_key=thread_id,
            route_id="source_" + new_ulid(),
            legacy_thread_id=thread_id,
            legacy_agent_id=agent_id,
            now=now_ms(),
        )


def resolve(
    services: SharedServices,
    owner_id: int,
    *,
    source_kind: str,
    source_instance: str,
    source_key: str,
    legacy_thread_id: str | None = None,
    legacy_agent_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> UnifiedSourceRoute:
    if legacy_thread_id:
        return legacy_route(services, owner_id, legacy_thread_id, legacy_agent_id)
    with services.db.transaction() as conn:
        return UnifiedSourceRouteRepo.ensure(
            conn,
            owner_id=owner_id,
            source_kind=source_kind,
            source_instance=source_instance,
            source_key=source_key,
            route_id="source_" + new_ulid(),
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
            now=now_ms(),
        )


def reset(services: SharedServices, owner_id: int, route_id: str, request_id: str) -> None:
    """Detach the next-send draft without creating an empty conversation."""
    with services.db.transaction() as conn:
        reset_in_transaction(services, conn, owner_id, route_id, request_id)


def reset_in_transaction(
    services: SharedServices, conn: Any, owner_id: int, route_id: str, request_id: str
) -> None:
    if not services.project_task_repo.lock_owner(conn, owner_id):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    route = UnifiedSourceRouteRepo.get(conn, owner_id, route_id)
    if route is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    require_writable(route)
    if route.state == "draft":
        return
    if route.task_id is None and route.revision > 1:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    if (
        route.task_id is not None
        and services.project_task_repo.get_in_transaction(conn, route.task_id, owner_id) is None
    ):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    task = (
        services.project_task_repo.get_in_transaction(conn, route.task_id, owner_id)
        if route.task_id
        else None
    )
    if task is not None:
        assert_project_writable(services, conn, owner_id, task.project_id)
    if task is not None and task.active_turn_id:
        raise OctopError.localized(ErrorCode.TASK_BUSY, status=409)
    if not UnifiedSourceRouteRepo.transition(
        conn,
        owner_id,
        route_id,
        route.revision,
        route.state,
        "draft",
        source_digest="draft:" + request_id,
        now=now_ms(),
    ):
        raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)


def bind_task(services: SharedServices, owner_id: int, route_id: str, task_id: str) -> None:
    """Publish an explicit owned target exactly once; a deleted binding is a tombstone."""
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        route = UnifiedSourceRouteRepo.get(conn, owner_id, route_id)
        if (
            route is None
            or services.project_task_repo.get_in_transaction(conn, task_id, owner_id) is None
        ):
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        require_writable(route)
        if route.task_id == task_id:
            return
        if route.task_id is not None or (route.state == "task" and route.revision > 1):
            raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
        if not UnifiedSourceRouteRepo.transition(
            conn,
            owner_id,
            route_id,
            route.revision,
            route.state,
            "task",
            task_id=task_id,
            now=now_ms(),
        ):
            raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
