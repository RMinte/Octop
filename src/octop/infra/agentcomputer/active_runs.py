"""In-process cancellation signal for one exact managed engine invocation.

This registry is owned by one server instance. A signal is best effort: task
cancellation does not prove that a remote model or child process has stopped.
The durable Run cancellation journal remains the source of truth.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from octop.infra.agentcomputer.cancellations import CancelRunReceipt
from octop.infra.agentcomputer.commands import CommandAdmissionError
from octop.infra.agentcomputer.models import EngineInvocation


@dataclass
class _ActiveRun:
    invocation: EngineInvocation
    task: asyncio.Task[object]
    signalled: bool = False


class ActiveRunRegistry:
    """Track only currently executing Run tasks, never a shared Octop thread."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active: dict[tuple[int, UUID], _ActiveRun] = {}

    @asynccontextmanager
    async def track(self, invocation: EngineInvocation) -> AsyncIterator[None]:
        """Bind the current task to the complete immutable invocation until exit."""
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if task is None or (self._loop is not None and self._loop is not loop):
            raise CommandAdmissionError("revisionConflict")
        intent = invocation.intent
        key = (intent.ownerID, intent.runID)
        if key in self._active or any(entry.task is task for entry in self._active.values()):
            raise CommandAdmissionError("revisionConflict")
        if self._loop is None:
            self._loop = loop
        entry = _ActiveRun(invocation=invocation, task=task)
        self._active[key] = entry
        try:
            yield
        finally:
            if self._active.get(key) is entry:
                del self._active[key]
            if not self._active:
                self._loop = None

    def signal_cancel(self, *, owner_id: int, receipt: CancelRunReceipt) -> bool:
        """Signal a matching active task after ``cancel_run`` commits its receipt.

        The caller supplies the authenticated owner because the immutable
        receipt does not contain an owner ID. False means no signal was sent;
        it does not change or reinterpret the durable receipt.
        """
        if receipt.status != "cancelRequested":
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if loop is not self._loop:
            return False
        entry = self._active.get((owner_id, receipt.runID))
        if entry is None or entry.signalled or entry.task.done():
            return False
        intent = entry.invocation.intent
        if (
            intent.ownerID != owner_id
            or intent.boxID != receipt.boxID
            or intent.runID != receipt.runID
            or intent.turnID != receipt.turnID
            or intent.taskID != receipt.taskID
            or intent.projectID != receipt.projectID
            or intent.restoreEpoch != receipt.restoreEpoch
            or intent.executionEpoch != receipt.executionEpoch
        ):
            return False
        if not entry.task.cancel():
            return False
        entry.signalled = True
        return True
