"""Process-scoped OS locks for bounded project preparation, without lease replay."""

from __future__ import annotations

import importlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from octop.infra.agentcomputer.commands import StorageDataError


@contextmanager
def preparation_lock(path: Path) -> Iterator[bool]:
    """Nonblocking lock. A stopped process releases it; a timeout never steals it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.resolve() != path:
        raise StorageDataError("invalid preparation lock location")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    module: Any = None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and info.st_mode & 0o077):
            raise StorageDataError("invalid preparation lock")
        if os.name == "nt":
            module = importlib.import_module("msvcrt")
            if not info.st_size:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                module.locking(fd, module.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            module = importlib.import_module("fcntl")
            try:
                module.flock(fd, module.LOCK_EX | module.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        yield acquired
    finally:
        if acquired:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                module.locking(fd, module.LK_UNLCK, 1)
            else:
                module.flock(fd, module.LOCK_UN)
        os.close(fd)
