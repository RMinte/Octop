"""Keep daemon and embedded conversation writers out of an offline restore."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from octop.infra.agentcomputer.bootstrap import DaemonBootstrap
from octop.infra.agents.resident_worker_lock import ResidentWorkerLock
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.paths import PathLayout


@contextmanager
def offline_restore_guard(paths: PathLayout) -> Iterator[None]:
    """Hold both lifetime locks until registry and filesystem restoration finishes."""
    controller = DaemonBootstrap(paths.agentcomputer_identity.parent)
    resident = ResidentWorkerLock(paths.root / "resident-worker.lock")
    with ExitStack() as cleanup:
        try:
            controller.acquire_sync()
        except OctopError as exc:
            if exc.code == ErrorCode.CONTROL_PLANE_BUSY:
                raise OctopError.localized(ErrorCode.BACKUP_OFFLINE_REQUIRED) from exc
            raise
        cleanup.callback(controller.close_sync)
        try:
            resident.acquire()
        except RuntimeError as exc:
            raise OctopError.localized(ErrorCode.BACKUP_OFFLINE_REQUIRED) from exc
        cleanup.callback(resident.release)
        yield
