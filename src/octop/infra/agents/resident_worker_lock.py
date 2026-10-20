"""One resident conversation writer per data directory, held for the process lifetime."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any


class ResidentWorkerLock:
    """Nonblocking OS lock. Elapsed time never grants another writer ownership."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None
        self._owner_pid: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            if self._owner_pid != os.getpid():
                raise RuntimeError("Resident worker lock cannot be reused after a process fork")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            module: Any
            if os.name == "nt":
                module = importlib.import_module("msvcrt")
                if not os.fstat(descriptor).st_size:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                module.locking(descriptor, module.LK_NBLCK, 1)
            else:
                module = importlib.import_module("fcntl")
                module.flock(descriptor, module.LOCK_EX | module.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            raise RuntimeError(
                "A resident conversation worker already owns this data directory"
            ) from exc
        except BaseException:
            os.close(descriptor)
            raise
        self._fd = descriptor
        self._owner_pid = os.getpid()

    def release(self) -> None:
        descriptor, self._fd = self._fd, None
        if descriptor is None:
            return
        try:
            # A forked child may close its inherited descriptor, but must not
            # unlock the parent's shared open-file description.
            if self._owner_pid != os.getpid():
                return
            module: Any
            if os.name == "nt":
                module = importlib.import_module("msvcrt")
                os.lseek(descriptor, 0, os.SEEK_SET)
                module.locking(descriptor, module.LK_UNLCK, 1)
            else:
                module = importlib.import_module("fcntl")
                module.flock(descriptor, module.LOCK_UN)
        finally:
            self._owner_pid = None
            os.close(descriptor)
