"""Fence native graph writes with the owning task's durable worker lease."""

from __future__ import annotations

import asyncio
import errno
import os
import sys
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, TypeVar

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    DeltaChannelHistory,
)

from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError

_T = TypeVar("_T")


@contextmanager
def checkpoint_fence(services: SharedServices, turn: ProjectTaskTurnRow) -> Iterator[None]:
    """Serialize one turn's native writes and lease replacement across processes.

    Call only from a background thread, before opening the main SQL transaction.
    The OS releases the lock if a worker process exits or crashes.
    """
    path = services.paths.root / "project-tasks" / turn.task_id / turn.turn_id / "checkpoint.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as lock:
        if sys.platform == "win32":
            import msvcrt

            if os.fstat(lock.fileno()).st_size == 0:
                try:
                    lock.write(b"\0")
                except OSError as exc:
                    # Another process can initialize and lock byte zero between
                    # our size check and write. Its initialized byte is sufficient.
                    if (
                        exc.errno not in {errno.EACCES, errno.EAGAIN}
                        or not os.fstat(lock.fileno()).st_size
                    ):
                        raise
            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    time.sleep(0.02)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class TaskCheckpointSaver(BaseCheckpointSaver[Any]):
    """Keep graph persistence and worker replacement under one per-turn file lock.

    The delegate owns a separate checkpoint store and remains on its original
    event loop. No SQL lock spans its asynchronous write. Cancellation drains an
    admitted write before returning, fencing out delayed writes from old leases.
    """

    def __init__(
        self,
        services: SharedServices,
        owner: int,
        turn: ProjectTaskTurnRow,
        delegate: BaseCheckpointSaver[Any],
    ) -> None:
        super().__init__(serde=delegate.serde)
        self.services, self.owner, self.turn, self.delegate = services, owner, turn, delegate
        self._write_lock = asyncio.Lock()

    @property
    def config_specs(self) -> list[Any]:
        return self.delegate.config_specs

    def get_next_version(self, current: Any, channel: None) -> Any:
        return self.delegate.get_next_version(current, channel)

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self.delegate.get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        return self.delegate.list(config, filter=filter, before=before, limit=limit)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await self.delegate.aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        async for item in self.delegate.alist(config, filter=filter, before=before, limit=limit):
            yield item

    def get_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        return self.delegate.get_delta_channel_history(config=config, channels=channels)

    async def aget_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        return await self.delegate.aget_delta_channel_history(config=config, channels=channels)

    def _protected(
        self, write: Callable[[], Coroutine[Any, Any, _T]], loop: asyncio.AbstractEventLoop
    ) -> _T:
        with checkpoint_fence(self.services, self.turn):
            with self.services.db.transaction() as conn:
                if not self.services.project_task_repo.lock_owner(conn, self.owner):
                    raise OctopError.localized(ErrorCode.FORBIDDEN)
                current = self.services.project_task_turn_repo.get_in_transaction(
                    conn, self.turn.turn_id
                )
                if (
                    current is None
                    or current.status != "running"
                    or current.cancel_requested
                    or not self.turn.worker_id
                    or current.worker_id != self.turn.worker_id
                ):
                    raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
            return asyncio.run_coroutine_threadsafe(write(), loop).result()

    async def _write(self, write: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        async with self._write_lock:
            loop = asyncio.get_running_loop()
            pending = asyncio.create_task(asyncio.to_thread(self._protected, write, loop))
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(pending)
                    break
                except asyncio.CancelledError:
                    if pending.done():
                        raise
                    cancelled = True
                except BaseException:
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise
            if cancelled:
                raise asyncio.CancelledError
            return result

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await self._write(
            lambda: self.delegate.aput(config, checkpoint, metadata, new_versions)
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await self._write(lambda: self.delegate.aput_writes(config, writes, task_id, task_path))

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        raise NotImplementedError("Task graph checkpoints require asynchronous execution")

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        raise NotImplementedError("Task graph checkpoints require asynchronous execution")
