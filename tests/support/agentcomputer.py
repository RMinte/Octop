"""Isolated control-plane fixtures; never enable production execution."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from octop.infra.agentcomputer.models import TaskCreateCommand, TaskCreateReceipt
from octop.infra.agentcomputer.tasks import register_task
from octop.infra.server import OctopServer

PREFIX = "/api/agentcomputer/v1"


async def register_ready_task(
    client: httpx.AsyncClient,
    server: OctopServer,
    auth: dict[str, str],
    owner_id: int,
) -> TaskCreateReceipt:
    """Use real stopped preparation; ready exists only in this temporary test DB."""
    project_id = uuid4()
    response = await client.post(
        f"{PREFIX}/projects",
        headers=auth,
        json={
            "commandID": str(uuid4()),
            "idempotencyKey": str(uuid4()),
            "expectedRestoreEpoch": 1,
            "projectID": str(project_id),
            "name": "Task mapping fixture",
            "configuration": {
                "schemaVersion": 1,
                "modelRef": "not-configured/no-model",
                "instructions": "Fixture only",
            },
        },
    )
    assert response.status_code == 201
    assert server.services is not None
    with server.services.db.transaction() as conn:
        # No production transition or HTTP Task-creation route does this.
        conn.execute(
            "UPDATE ac_projects SET preparation_state='ready' WHERE project_id=? AND owner_id=?",
            (str(project_id), owner_id),
        )
        server.services.ac_project_preparation_repo.clear_reason(
            conn, str(project_id), owner_id, updated_at=int(datetime.now(UTC).timestamp() * 1000)
        )
    command = TaskCreateCommand(
        commandID=uuid4(),
        idempotencyKey=str(uuid4()),
        expectedRestoreEpoch=1,
        projectID=project_id,
        expectedProjectRevision=1,
        taskID=uuid4(),
        title="  Fixture task 中文  ",
    )
    return await asyncio.to_thread(
        register_task, server.services, owner_id=owner_id, command=command
    )
