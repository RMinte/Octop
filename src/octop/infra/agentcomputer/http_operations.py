"""Keep AgentComputer HTTP work alive until its server-owned resources close."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from octop.infra.agentcomputer.commands import CommandAdmissionError

logger = logging.getLogger(__name__)


class HttpOperationRegistry:
    """Track HTTP work independently of the request task that started it."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._operations: set[asyncio.Task[Any]] = set()
        self._detached: set[asyncio.Task[Any]] = set()
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    def _start[T](self, operation: Callable[[], Coroutine[Any, Any, T]]) -> asyncio.Task[T]:
        loop = asyncio.get_running_loop()
        if self._closing:
            raise CommandAdmissionError("runtimeUnavailable")
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("AgentComputer HTTP registry belongs to another event loop")
        self._loop = loop
        task = loop.create_task(operation())
        self._operations.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        detached = task in self._detached
        self._detached.discard(task)
        self._operations.discard(task)
        if not self._operations:
            self._loop = None
        self._observe(task, detached=detached)

    @staticmethod
    def _observe(task: asyncio.Task[Any], *, detached: bool) -> None:
        if task.cancelled():
            if detached:
                logger.warning("AgentComputer HTTP operation was cancelled after request exit")
            return
        error = task.exception()
        if detached and error is not None:
            logger.warning(
                "AgentComputer HTTP operation failed after request exit: %s",
                type(error).__name__,
            )

    async def _wait[T](self, task: asyncio.Task[T]) -> T:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            owner = asyncio.current_task()
            if owner is not None and owner.cancelling():
                if task.done():
                    self._observe(task, detached=True)
                else:
                    self._detached.add(task)
            raise

    async def run_thread[T](self, func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        task = self._start(lambda: asyncio.to_thread(func, *args, **kwargs))
        return await self._wait(task)

    async def run_async[T](self, operation: Callable[[], Coroutine[Any, Any, T]]) -> T:
        task = self._start(operation)
        return await self._wait(task)

    async def drain(self) -> None:
        """Close admission and observe existing work without cancelling its thread."""
        self._closing = True
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("AgentComputer HTTP registry belongs to another event loop")
        tasks = tuple(self._operations)
        if asyncio.current_task() in tasks:
            raise RuntimeError("an AgentComputer HTTP operation cannot drain itself")
        if tasks:
            waiter = asyncio.gather(*tasks, return_exceptions=True)
            while not waiter.done():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    continue
            waiter.result()
