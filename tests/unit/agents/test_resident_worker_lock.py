"""OS ownership excludes live peers and is released after process termination."""

from __future__ import annotations

import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from octop.infra.agents.resident_worker_lock import ResidentWorkerLock


def _hold_worker_lock(path: str, connection: Connection) -> None:
    lock = ResidentWorkerLock(Path(path))
    try:
        lock.acquire()
        connection.send("acquired")
        connection.recv()
    finally:
        connection.close()
    # Deliberately leave the descriptor open until the process exits.


def test_same_instance_is_idempotent_and_other_instances_are_excluded(tmp_path: Path) -> None:
    path = tmp_path / "resident-worker.lock"
    first, second = ResidentWorkerLock(path), ResidentWorkerLock(path)
    try:
        first.acquire()
        first.acquire()
        with pytest.raises(RuntimeError, match="already owns"):
            second.acquire()
        second.release()
        first.release()
        first.release()
        second.acquire()
        with pytest.raises(RuntimeError, match="already owns"):
            first.acquire()
    finally:
        first.release()
        second.release()


@pytest.mark.parametrize("terminate", [False, True], ids=["normal-exit", "terminated"])
def test_live_process_ownership_and_os_release(tmp_path: Path, terminate: bool) -> None:
    path = tmp_path / "resident-worker.lock"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_worker_lock, args=(str(path), child))
    contender = ResidentWorkerLock(path)
    try:
        process.start()
        child.close()
        assert parent.poll(15), "worker did not acquire its lock"
        assert parent.recv() == "acquired"
        assert process.is_alive()
        with pytest.raises(RuntimeError, match="already owns"):
            contender.acquire()
        if terminate:
            process.terminate()
        else:
            parent.send("exit")
        process.join(15)
        assert not process.is_alive()
        if not terminate:
            assert process.exitcode == 0
        contender.acquire()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(15)
        parent.close()
        child.close()
        contender.release()
