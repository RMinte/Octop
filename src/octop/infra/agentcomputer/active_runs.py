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
from octop.infra.agentcomputer.models import DispatchIntent, EngineInvocation, RunClaim


@dataclass
class _ActiveRun:
    intent: DispatchIntent
    claimant_id: UUID
    task: asyncio.Task[object]
    signalled: bool = False


class ActiveRunRegistry:
    """Track only currently executing Run tasks, never a shared Octop thread."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active: dict[tuple[int, UUID], _ActiveRun] = {}
        self._dispatches: dict[tuple[int, UUID], _ActiveRun] = {}
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    def _current_task(self) -> asyncio.Task[object]:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if self._closing or task is None or (self._loop is not None and self._loop is not loop):
            raise CommandAdmissionError("revisionConflict")
        if self._loop is None:
            self._loop = loop
        return task

    def _release_loop(self) -> None:
        if not self._active and not self._dispatches:
            self._loop = None

    @asynccontextmanager
    async def track_dispatch(self, claim: RunClaim) -> AsyncIterator[None]:
        """Own the complete dispatch task, including its durable failure fence."""
        task = self._current_task()
        intent = claim.intent
        key = (intent.ownerID, intent.runID)
        if (
            key in self._dispatches
            or key in self._active
            or any(entry.task is task for entry in self._dispatches.values())
            or any(entry.task is task for entry in self._active.values())
        ):
            raise CommandAdmissionError("revisionConflict")
        entry = _ActiveRun(intent=intent, claimant_id=claim.claimantID, task=task)
        self._dispatches[key] = entry
        try:
            yield
        finally:
            if self._dispatches.get(key) is entry:
                del self._dispatches[key]
            self._release_loop()

    @asynccontextmanager
    async def track(self, invocation: EngineInvocation) -> AsyncIterator[None]:
        """Bind the current task to the complete immutable invocation until exit."""
        task = self._current_task()
        intent = invocation.intent
        key = (intent.ownerID, intent.runID)
        if key in self._active or any(entry.task is task for entry in self._active.values()):
            raise CommandAdmissionError("revisionConflict")
        dispatch = self._dispatches.get(key)
        if dispatch is not None and (
            dispatch.task is not task
            or dispatch.intent != intent
            or dispatch.claimant_id != invocation.claimantID
        ):
            raise CommandAdmissionError("revisionConflict")
        if any(entry.task is task and entry is not dispatch for entry in self._dispatches.values()):
            raise CommandAdmissionError("revisionConflict")
        entry = _ActiveRun(intent=intent, claimant_id=invocation.claimantID, task=task)
        self._active[key] = entry
        try:
            yield
        finally:
            if self._active.get(key) is entry:
                del self._active[key]
            self._release_loop()

    async def drain(self) -> None:
        """Close registration, signal owned tasks and wait for their full exit."""
        self._closing = True
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("active Run registry belongs to another event loop")
        tasks = {entry.task for entry in (*self._dispatches.values(), *self._active.values())}
        if asyncio.current_task() in tasks:
            raise RuntimeError("an active Run cannot drain itself")
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            waiter = asyncio.gather(*tasks, return_exceptions=True)
            while not waiter.done():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    continue
            waiter.result()

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
        entry = self._dispatches.get((owner_id, receipt.runID))
        if entry is None:
            entry = self._active.get((owner_id, receipt.runID))
        if entry is None or entry.signalled or entry.task.done():
            return False
        intent = entry.intent
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
