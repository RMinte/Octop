"""Ref-counted embedded OctopServer for reuse within one asyncio event loop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from octop.infra.db.services import SharedServices
from octop.infra.server import OctopServer

if TYPE_CHECKING:
    from octop.infra.projects.conversation_migrations import ConversationMigrationWorker
    from octop.infra.projects.runtime import ProjectTaskWorker

_lock = asyncio.Lock()
_server: OctopServer | None = None
_refs = 0


@asynccontextmanager
async def embedded_runtime() -> AsyncIterator[OctopServer]:
    """Boot OctopServer once per event loop; nested ``async with`` shares it."""
    global _server, _refs

    async with _lock:
        if _server is None:
            _server = OctopServer()
            await _server.start()
        _refs += 1
        server = _server

    try:
        yield server
    finally:
        async with _lock:
            _refs -= 1
            if _refs == 0 and _server is not None:
                await _server.stop()
                _server = None


@asynccontextmanager
async def embedded_chat_server() -> AsyncIterator[OctopServer]:
    """Alias for :func:`embedded_runtime` (one server per nested scope)."""
    async with embedded_runtime() as server:
        yield server


@dataclass(frozen=True)
class TaskCliRuntime:
    services: SharedServices
    project_task_worker: ProjectTaskWorker
    conversation_migration_worker: ConversationMigrationWorker


@asynccontextmanager
async def embedded_task_runtime() -> AsyncIterator[TaskCliRuntime]:
    """Run only durable Task workers, without booting IM, cron or legacy agents."""
    from octop.cli.support.db import open_cli_services
    from octop.infra.projects.conversation_migrations import ConversationMigrationWorker
    from octop.infra.projects.ingress import TaskIngressWorker
    from octop.infra.projects.runtime import ProjectTaskWorker

    with open_cli_services() as services:
        worker = ProjectTaskWorker(services)
        migration = ConversationMigrationWorker(services)
        inbox = TaskIngressWorker(services, task_worker=worker)
        try:
            await worker.start()
            await inbox.start()
            migration.start()
            yield TaskCliRuntime(services, worker, migration)
        finally:
            await migration.close()
            await inbox.close()
            # Worker shutdown releases its lease. It never sends Task cancel;
            # another worker can recover the accepted turn from persistence.
            await worker.close()
