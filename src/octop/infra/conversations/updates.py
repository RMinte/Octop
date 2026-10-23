"""Loop-local wakeups for committed conversation state; persistence stays authoritative."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Hashable


class CommittedUpdates[Key: Hashable]:
    """Watch committed changes without retaining output or owning execution.

    Callers notify only after a successful commit. Watches and notifications run
    on the same event loop; database polling covers other processes and writers.
    """

    def __init__(self) -> None:
        self.listeners: dict[Key, set[Callable[[], None]]] = {}

    def watch(self, key: Key, wake: Callable[[], None]) -> Callable[[], None]:
        listeners = self.listeners.setdefault(key, set())
        listeners.add(wake)

        def stop() -> None:
            listeners.discard(wake)
            if not listeners and self.listeners.get(key) is listeners:
                self.listeners.pop(key)

        return stop

    def notify(self, key: Key) -> None:
        # A callback may remove its own watch during notification.
        for wake in tuple(self.listeners.get(key, ())):
            wake()


class PendingUpdates[Key: Hashable]:
    """Coalesce wakeups while preserving commits arriving during a snapshot read.

    Take the pending keys before awaiting the authoritative read. A later add
    then belongs to the next batch and keeps that batch's wakeup set.
    """

    def __init__(self) -> None:
        self._pending: set[Key] = set()
        self._ready = asyncio.Event()

    def add(self, key: Key) -> None:
        self._pending.add(key)
        self._ready.set()

    def discard(self, key: Key) -> None:
        self._pending.discard(key)
        if not self._pending:
            self._ready.clear()

    def take(self) -> set[Key]:
        pending, self._pending = self._pending, set()
        self._ready.clear()
        return pending

    async def wait(self, timeout: float) -> bool:
        if self._ready.is_set():
            return True
        if timeout <= 0:
            return False
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            return False
        return True
