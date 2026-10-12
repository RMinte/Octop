"""Lifetime ownership cannot be stolen, including during cancelled recovery."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from threading import Event
from unittest.mock import Mock

import pytest

from octop.infra.agentcomputer.bootstrap import DaemonBootstrap
from octop.infra.errors import ErrorCode, OctopError


async def test_daemon_lock_survives_other_process_and_can_be_reacquired(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    guard = DaemonBootstrap(tmp_path / "agentcomputer-control")
    await guard.acquire()
    await guard.acquire()
    code = """
import sys
from pathlib import Path
from octop.infra.agentcomputer.preparation_lock import preparation_lock
with preparation_lock(Path(sys.argv[1])) as acquired:
    print(int(acquired))
"""
    try:
        output = await asyncio.to_thread(
            subprocess.check_output,
            [sys.executable, "-c", code, str(tmp_path / "agentcomputer-control/controller.lock")],
            text=True,
            timeout=10,
        )
        assert output.strip() == "0"
    finally:
        await guard.close()
    second = DaemonBootstrap(tmp_path / "agentcomputer-control")
    await second.acquire()
    await second.close()
    await second.close()


async def test_busy_guard_does_not_release_owner(tmp_path):
    owner = DaemonBootstrap(tmp_path / "control")
    other = DaemonBootstrap(tmp_path / "control")
    await owner.acquire()
    try:
        for _ in range(2):
            with pytest.raises(OctopError) as caught:
                await other.acquire()
            assert caught.value.code == ErrorCode.CONTROL_PLANE_BUSY
            await other.close()
    finally:
        await owner.close()
    await other.acquire()
    await other.close()


async def test_recovery_requires_owned_daemon_guard(tmp_path):
    guard = DaemonBootstrap(tmp_path / "control")
    with pytest.raises(RuntimeError, match="lifetime ownership"):
        await guard.recover(Mock())


async def test_cancelled_acquire_waits_for_os_operation_and_releases(tmp_path, monkeypatch):
    guard = DaemonBootstrap(tmp_path / "control")
    acquired, release = Event(), Event()
    original = guard._open_lock

    def delayed():
        result = original()
        acquired.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(guard, "_open_lock", delayed)
    task = asyncio.create_task(guard.acquire())
    try:
        assert await asyncio.to_thread(acquired.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await guard.close()
    other = DaemonBootstrap(tmp_path / "control")
    await other.acquire()
    await other.close()


async def test_cancelled_recovery_settles_before_lifetime_owner_can_release(tmp_path, monkeypatch):
    from octop.infra.agentcomputer import recovery

    started, release, finished = Event(), Event(), Event()

    def delayed(_services):
        started.set()
        assert release.wait(5)
        finished.set()
        return recovery.RecoveryReport(scanned=0, tightened=0, unchanged=0)

    monkeypatch.setattr(recovery, "recover_dispatches", delayed)
    control_directory = tmp_path / "agentcomputer-control"
    guard = DaemonBootstrap(control_directory)
    await guard.acquire()
    services = Mock()
    services.paths.agentcomputer_identity = control_directory / "identity.json"
    task = asyncio.create_task(guard.recover(services))
    other = DaemonBootstrap(control_directory)
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(OctopError):
            await other.acquire()
        assert not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    finally:
        release.set()
        await guard.close()
    await other.acquire()
    await other.close()
