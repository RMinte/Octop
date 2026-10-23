"""Stop a project's execution before permanently removing its conversations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import service
from octop.infra.projects.runtime import ProjectTaskWorker


def _prepare(services: SharedServices, owner_id: int, project_id: str) -> list[str]:
    with services.db.transaction() as conn:
        projects = services.project_repo
        if not projects.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        if projects.get_in_transaction(conn, project_id, owner_id) is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        projects.mark_deleting(conn, owner_id, project_id)
        tasks = services.project_task_repo.for_project(conn, project_id, owner_id)
        for task in tasks:
            if not task.active_turn_id:
                continue
            turn = services.project_task_turn_repo.get_in_transaction(conn, task.active_turn_id)
            if turn is not None and turn.status in {"queued", "running"}:
                # A claimed tool can still return its result, but no next tool
                # may be dispatched while the project is being stopped.
                services.project_task_turn_repo.save_request(
                    conn, turn.turn_id, turn.requests_json, True
                )
        return [task.task_id for task in tasks]


async def delete_project(
    services: SharedServices,
    worker: ProjectTaskWorker | None,
    owner_id: int,
    project_id: str,
    *,
    close_sessions: Callable[[str], Awaitable[None]],
) -> None:
    task_ids = await asyncio.to_thread(_prepare, services, owner_id, project_id)
    if worker is not None and not await worker.stop_for_deletion(owner_id, task_ids):
        raise OctopError.localized(ErrorCode.PROJECT_TASKS_BUSY)
    for task_id in task_ids:
        await close_sessions(task_id)
    # The durable project fence remains until this final transaction commits.
    # A timeout, disconnect, or cleanup failure retains all conversation rows;
    # a later DELETE can continue the same operation.
    await asyncio.to_thread(service.delete_project, services, owner_id, project_id)
