"""Common streaming boundary for native and external task engines."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices

TaskEvents = AsyncGenerator[dict[str, Any], None]
NativeRunner = Callable[[SharedServices, int, ProjectTaskTurnRow, list[Any]], TaskEvents]


@dataclass(frozen=True)
class EngineRequest:
    services: SharedServices
    owner_id: int
    turn: ProjectTaskTurnRow
    history: list[Any]


class TaskEngine(Protocol):
    def stream(self, request: EngineRequest) -> TaskEvents: ...


class NativeTaskEngine:
    def __init__(self, runner: NativeRunner) -> None:
        self.runner = runner

    async def stream(self, request: EngineRequest) -> TaskEvents:
        async with contextlib.aclosing(
            self.runner(request.services, request.owner_id, request.turn, request.history)
        ) as events:
            async for event in events:
                yield event


class ACPTaskEngine:
    async def stream(self, request: EngineRequest) -> TaskEvents:
        from octop.infra.projects.engines.runtime import run_acp_turn

        async with contextlib.aclosing(run_acp_turn(request)) as events:
            async for event in events:
                yield event
