"""Serialize Task expert-memory operations with expert store maintenance."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path


@contextlib.contextmanager
def scope_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as lock:
        if sys.platform == "win32":
            import msvcrt

            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b"\0")
            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
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


def store_lock(root: Path, expert_id: str) -> contextlib.AbstractContextManager[None]:
    identity = hashlib.sha256(expert_id.encode()).hexdigest()
    return scope_lock(root / "expert-memory-locks" / f"store-{identity}.lock")


@contextlib.asynccontextmanager
async def maintenance_lock(root: Path, expert_id: str) -> AsyncIterator[None]:
    guard = store_lock(root, expert_id)
    entering = asyncio.create_task(asyncio.to_thread(guard.__enter__))
    try:
        await asyncio.shield(entering)
    except asyncio.CancelledError:
        await entering
        await asyncio.to_thread(guard.__exit__, None, None, None)
        raise
    try:
        yield
    finally:
        await asyncio.to_thread(guard.__exit__, None, None, None)
