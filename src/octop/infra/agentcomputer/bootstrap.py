"""Daemon lifetime ownership and conservative recovery before runtime boot.

This lock coordinates this installation's new daemon processes. It does not
prove that an older or external worker stopped, and never grants execution.
Embedded servers do not construct this bootstrap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any

from octop.infra.agentcomputer.commands import StorageDataError
from octop.infra.agentcomputer.preparation_lock import preparation_lock
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError


async def settle_task[T](task: asyncio.Task[T]) -> tuple[T, bool]:
    """Observe a background operation before cancelling its owner or releasing its lock."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def settled_to_thread[T](func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Let a DB thread finish before propagating cancellation to its owner."""
    result, cancelled = await settle_task(
        asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    )
    if cancelled:
        raise asyncio.CancelledError
    return result


class DaemonBootstrap:
    def __init__(self, control_directory: Path) -> None:
        self._directory = control_directory
        self._lock: AbstractContextManager[bool] | None = None

    def _open_lock(self) -> tuple[AbstractContextManager[bool], bool]:
        context = preparation_lock(self._directory.resolve() / "controller.lock")
        return context, context.__enter__()

    def acquire_sync(self) -> None:
        """CLI must own the lock before persisting options or generating TLS files."""
        if self._lock is not None:
            return
        # Resolve the install directory, then use the existing private OS lock
        # primitive. The file is never unlinked or reclaimed by age/PID/lease.
        context, acquired = self._open_lock()
        self._lock = context
        if not acquired:
            self.close_sync()
            raise OctopError.localized(ErrorCode.CONTROL_PLANE_BUSY)

    async def acquire(self) -> None:
        _, cancelled = await settle_task(asyncio.create_task(asyncio.to_thread(self.acquire_sync)))
        if cancelled:
            await self.close()
            raise asyncio.CancelledError

    def __enter__(self) -> DaemonBootstrap:
        self.acquire_sync()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close_sync()

    async def recover(self, services: SharedServices) -> None:
        from octop.infra.agentcomputer.recovery import recover_dispatches

        if self._lock is None:
            raise RuntimeError("daemon recovery requires lifetime ownership")

        def recover_owned() -> None:
            if self._directory.resolve() != services.paths.agentcomputer_identity.parent.resolve():
                raise StorageDataError("daemon ownership installation mismatch")
            recover_dispatches(services)

        _, cancelled = await settle_task(asyncio.create_task(asyncio.to_thread(recover_owned)))
        if cancelled:
            raise asyncio.CancelledError

    def close_sync(self) -> None:
        context, self._lock = self._lock, None
        if context is not None:
            context.__exit__(None, None, None)

    async def close(self) -> None:
        _, cancelled = await settle_task(asyncio.create_task(asyncio.to_thread(self.close_sync)))
        if cancelled:
            raise asyncio.CancelledError
