"""Real server roles reconcile before runtime boot and preserve embedded use."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from threading import Event
from unittest.mock import Mock

import pytest

from octop.infra.agentcomputer import dispatch
from octop.infra.agentcomputer.bootstrap import DaemonBootstrap
from octop.infra.agentcomputer.commands import StorageDataError
from octop.infra.agentcomputer.runs import get_run
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from tests.support.app import ensure_control_plane_bound
from tests.unit.agentcomputer.test_dispatch import seed
from tests.unit.agentcomputer.test_submission_runs import control as control


@pytest.mark.parametrize("stage", ["claimed", "dispatching", "engineAccepted"])
@pytest.mark.parametrize("daemon", [False, True])
async def test_server_role_fences_only_owned_daemon_bootstrap(control, monkeypatch, stage, daemon):
    services, receipt, claim = seed(control, monkeypatch)
    if stage != "claimed":
        invocation = await asyncio.to_thread(dispatch._begin_dispatch, services, claim)
        if stage == "engineAccepted":
            await asyncio.to_thread(dispatch.accept_engine_entry, services, invocation)
    before = get_run(services, owner_id=1, run_id=receipt.runID)
    original = services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json
    server = OctopServer(
        home=services.paths.root,
        daemon_bootstrap=DaemonBootstrap(services.paths.agentcomputer_identity.parent)
        if daemon
        else None,
    )
    try:
        await server.start()
        await ensure_control_plane_bound(server)
        assert server.services is not None
        view = get_run(server.services, owner_id=1, run_id=receipt.runID)
        assert view.dispatchPhase == stage and view.executionEpoch == 1
        assert view.state == ("interrupted" if daemon else "queued")
        assert view.reconciliationRequired is daemon
        assert view.revision == before.revision + int(daemon)
        with services.db.connect() as conn:
            assert conn.execute("SELECT active_run_id FROM ac_tasks").fetchone()[0] == str(
                receipt.runID
            )
            assert conn.execute("SELECT active_run_id FROM ac_projects").fetchone()[0] == str(
                receipt.runID
            )
        assert services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json == original
        assert server.app_runtime is not None
        assert server.app_runtime.agent_registry.list_rows() == []
        await server.start()
        assert get_run(services, owner_id=1, run_id=receipt.runID) == view
    finally:
        await server.stop()


async def test_second_daemon_refuses_same_home_but_embedded_can_start(tmp_octop_home):
    first = OctopServer(
        home=tmp_octop_home,
        daemon_bootstrap=DaemonBootstrap(tmp_octop_home / "agentcomputer-control"),
    )
    second = OctopServer(
        home=tmp_octop_home,
        daemon_bootstrap=DaemonBootstrap(tmp_octop_home / "agentcomputer-control"),
    )
    embedded = OctopServer(home=tmp_octop_home)
    try:
        await first.start()
        with pytest.raises(OctopError) as caught:
            await second.start()
        assert caught.value.code == ErrorCode.CONTROL_PLANE_BUSY
        await second.stop()
        await embedded.start()
        await embedded.stop()
        with pytest.raises(OctopError):
            await second.start()
    finally:
        await first.stop()
        await embedded.stop()
    await second.start()
    await second.stop()


async def test_same_server_restart_reopens_dispatch_admission(control, monkeypatch):
    services, _, _ = control
    server = OctopServer(
        home=services.paths.root,
        daemon_bootstrap=DaemonBootstrap(services.paths.agentcomputer_identity.parent),
    )
    try:
        await server.start()
        await ensure_control_plane_bound(server)
        old_registry = server.agentcomputer_active_runs
        await server.stop()
        assert old_registry.closing
        await server.start()
        await ensure_control_plane_bound(server)
        assert server.services is not None
        assert server.agentcomputer_active_runs is not old_registry
        assert not server.agentcomputer_active_runs.closing
        _, receipt, claim = seed(control, monkeypatch)

        async def invoke(invocation):
            await asyncio.to_thread(dispatch.accept_engine_entry, server.services, invocation)

        entry = await dispatch.dispatch_run(
            server.services,
            claim=claim,
            invoke=invoke,
            active_runs=server.agentcomputer_active_runs,
        )
        assert entry.runID == receipt.runID
        view = get_run(services, owner_id=1, run_id=receipt.runID)
        assert view.dispatchPhase == "engineAccepted" and not view.reconciliationRequired
    finally:
        await server.stop()


async def test_bad_recovery_stops_before_registry_boot_and_releases_guard(control, monkeypatch):
    services, receipt, claim = seed(control, monkeypatch)
    await asyncio.to_thread(dispatch._begin_dispatch, services, claim)
    with services.db.transaction() as conn:
        conn.execute("DELETE FROM ac_run_dispatches WHERE run_id=?", (str(receipt.runID),))
    registry = Mock(side_effect=AssertionError("bad recovery must precede runtime creation"))
    monkeypatch.setattr("octop.infra.server.AgentManager", registry)
    server = OctopServer(
        home=services.paths.root,
        daemon_bootstrap=DaemonBootstrap(services.paths.agentcomputer_identity.parent),
    )
    try:
        with pytest.raises(StorageDataError):
            await server.start()
            await ensure_control_plane_bound(server)
        assert server.services is None and server.app_runtime is None
        registry.assert_not_called()
    finally:
        await server.stop()
    other = DaemonBootstrap(services.paths.agentcomputer_identity.parent)
    await other.acquire()
    await other.close()


@pytest.mark.parametrize("component", ["gateway", "cron", "registry", "users", "proactive"])
async def test_partial_runtime_boot_closes_created_components_before_releasing_guard(
    control, monkeypatch, component
):
    from octop.infra.agents.manager import AgentManager
    from octop.infra.cron.manager import CronManager
    from octop.infra.gateway.gateway import Gateway
    from octop.infra.proactive.scheduler import ProactiveCareScheduler
    from octop.infra.users.manager import UserManager

    services, _, _ = control
    server = OctopServer(
        home=services.paths.root,
        daemon_bootstrap=DaemonBootstrap(services.paths.agentcomputer_identity.parent),
    )
    lifecycle = {
        "gateway": (Gateway, "boot", "shutdown"),
        "cron": (CronManager, "boot", "shutdown"),
        "registry": (AgentManager, "boot", "shutdown"),
        "users": (UserManager, "boot", "shutdown_all"),
        "proactive": (ProactiveCareScheduler, "start_all", "shutdown"),
    }
    closed = []
    opened = []
    for name, (kind, _, shutdown_name) in lifecycle.items():
        original = getattr(kind, shutdown_name)

        def make_close(label, method):
            async def close(instance):
                await method(instance)
                closed.append(label)

            return close

        monkeypatch.setattr(kind, shutdown_name, make_close(name, original))

    async def fail(_instance):
        assert server.services is not None
        opened.append(server.services.db)
        raise RuntimeError("fixture partial boot failure")

    kind, boot_name, _ = lifecycle[component]
    monkeypatch.setattr(kind, boot_name, fail)
    with pytest.raises(RuntimeError, match="partial boot"):
        await server.start()
        await ensure_control_plane_bound(server)
    assert component in closed and "registry" in closed
    assert server.services is None and server.app_runtime is None
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError), opened[0].connect() as conn:
        conn.execute("SELECT 1")
    other = DaemonBootstrap(services.paths.agentcomputer_identity.parent)
    await other.acquire()
    await other.close()
    await server.stop()


@pytest.mark.parametrize("operation", ["stop", "startFailure"])
async def test_pool_close_is_settled_despite_repeated_cancel_before_guard_release(
    control, monkeypatch, operation
):
    services, _, _ = control
    server = OctopServer(
        home=services.paths.root,
        daemon_bootstrap=DaemonBootstrap(services.paths.agentcomputer_identity.parent),
    )
    closing, release, finished = Event(), Event(), Event()

    def install_delayed_close():
        assert server.services is not None
        original = server.services.db.close

        def delayed():
            closing.set()
            assert release.wait(5)
            original()
            finished.set()

        monkeypatch.setattr(server.services.db, "close", delayed)

    if operation == "stop":
        await server.start()
        await ensure_control_plane_bound(server)
        install_delayed_close()
        task = asyncio.create_task(server.stop())
    else:

        def fail():
            install_delayed_close()
            raise RuntimeError("fixture JWT startup failure")

        monkeypatch.setattr(server, "_ensure_jwt_secret", fail)
        task = asyncio.create_task(server.start())
    other = DaemonBootstrap(services.paths.agentcomputer_identity.parent)
    try:
        assert await asyncio.to_thread(closing.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(OctopError):
            await other.acquire()
        assert not finished.is_set() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert server.services is None and server.app_runtime is None
    finally:
        release.set()
        await server.stop()
    await other.acquire()
    await other.close()


async def test_server_stop_drains_dispatch_fence_before_pool_and_guard_close(control, monkeypatch):
    services, _, _ = control
    server = OctopServer(
        home=services.paths.root,
        daemon_bootstrap=DaemonBootstrap(services.paths.agentcomputer_identity.parent),
    )
    other = DaemonBootstrap(services.paths.agentcomputer_identity.parent)
    release = Event()
    run_task = stop_task = None
    try:
        await server.start()
        await ensure_control_plane_bound(server)
        assert server.services is not None
        server_pool = server.services.db
        _, receipt, claim = seed(control, monkeypatch)
        original_receipt = services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json
        entered = asyncio.Event()
        fencing = Event()
        original_fence = dispatch.interrupt_uncertain_run

        def delayed_fence(services, *, claim):
            fencing.set()
            assert release.wait(timeout=10)
            with server_pool.connect() as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
            return original_fence(services, claim=claim)

        async def invoke(_invocation):
            entered.set()
            await asyncio.Future()

        monkeypatch.setattr(dispatch, "interrupt_uncertain_run", delayed_fence)
        run_task = asyncio.create_task(
            dispatch.dispatch_run(
                server.services,
                claim=claim,
                invoke=invoke,
                active_runs=server.agentcomputer_active_runs,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        stop_task = asyncio.create_task(server.stop())
        assert await asyncio.to_thread(fencing.wait, 10)
        stop_task.cancel()
        await asyncio.sleep(0)
        stop_task.cancel()
        assert not stop_task.done() and not run_task.done()
        assert server.services is not None
        with server_pool.connect() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(OctopError) as busy:
            await other.acquire()
        assert busy.value.code == ErrorCode.CONTROL_PLANE_BUSY
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stop_task
        with pytest.raises(asyncio.CancelledError):
            await run_task
        assert server.services is None and server.app_runtime is None
        with pytest.raises(sqlite3.ProgrammingError), server_pool.connect() as conn:
            conn.execute("SELECT 1")
        view = get_run(services, owner_id=1, run_id=receipt.runID)
        assert view.state == "interrupted" and view.reconciliationRequired
        assert view.dispatchPhase == "dispatching"
        assert (
            services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json == original_receipt
        )
        task = services.ac_task_repo.get(str(receipt.taskID), 1)
        assert task.active_run_id == str(receipt.runID)
        assert services.ac_project_repo.get(task.project_id, 1).active_run_id == str(receipt.runID)
        await other.acquire()
    finally:
        release.set()
        if run_task is not None and not run_task.done():
            run_task.cancel()
            with suppress(asyncio.CancelledError):
                await run_task
        if stop_task is not None and not stop_task.done():
            with suppress(asyncio.CancelledError):
                await stop_task
        await server.stop()
        await other.close()
