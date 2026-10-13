"""Server shutdown settles AgentComputer HTTP work beyond its request lifetime."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from threading import Event
from uuid import uuid4

import httpx
import pytest

from octop.api.app import build_app
from octop.infra.agentcomputer.bootstrap import DaemonBootstrap
from octop.infra.agentcomputer.models import ProjectCreateReceipt, ProjectView
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from tests.support.app import ensure_control_plane_bound
from tests.support.auth import auth_header, bootstrap_admin

PREFIX = "/api/agentcomputer/v1"


def project_command() -> dict[str, object]:
    return {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "projectID": str(uuid4()),
        "name": "Shutdown project fixture",
        "configuration": {
            "schemaVersion": 1,
            "modelRef": "not-configured/no-model",
            "instructions": "Fixture only",
        },
    }


async def _wait_for_http_shutdown(server: OctopServer) -> None:
    while not server.agentcomputer_http_operations.closing:
        await asyncio.sleep(0)


async def test_cancelled_project_acceptance_finishes_preparation_before_stop_and_restarts(
    tmp_octop_home, monkeypatch
):
    server = OctopServer(
        home=tmp_octop_home,
        daemon_bootstrap=DaemonBootstrap(tmp_octop_home / "agentcomputer-control"),
    )
    other = DaemonBootstrap(tmp_octop_home / "agentcomputer-control")
    entered, release, close_attempted = Event(), Event(), Event()
    request = stop = None
    try:
        await server.start()
        await ensure_control_plane_bound(server)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_app(server)),
            base_url="http://testserver",
        ) as client:
            await bootstrap_admin(client, tmp_octop_home)
            auth = await auth_header(client)
            assert server.services is not None
            services = server.services
            pool = services.db
            original_insert = services.ac_command_repo.insert
            original_close = pool.close

            def delayed_insert(*args, **kwargs):
                # The project and preparation rows exist in this uncommitted transaction.
                entered.set()
                assert release.wait(timeout=10)
                return original_insert(*args, **kwargs)

            def observed_close():
                close_attempted.set()
                original_close()

            monkeypatch.setattr(services.ac_command_repo, "insert", delayed_insert)
            monkeypatch.setattr(pool, "close", observed_close)
            body = project_command()
            request = asyncio.create_task(
                client.post(f"{PREFIX}/projects", headers=auth, json=body)
            )
            assert await asyncio.to_thread(entered.wait, 10)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request

            stop = asyncio.create_task(server.stop())
            await asyncio.wait_for(_wait_for_http_shutdown(server), timeout=5)
            assert not stop.done() and not close_attempted.is_set()
            assert server.services is services
            with pytest.raises(OctopError) as busy:
                await other.acquire()
            assert busy.value.code == ErrorCode.CONTROL_PLANE_BUSY

            release.set()
            await asyncio.wait_for(stop, timeout=10)
            assert close_attempted.is_set() and server.services is None
            with sqlite3.connect(pool.path) as conn:
                row = conn.execute(
                    "SELECT receipt_json FROM ac_commands WHERE command_id=?",
                    (str(body["commandID"]),),
                ).fetchone()
                assert row is not None
                receipt = ProjectCreateReceipt.model_validate_json(row[0])
                assert str(receipt.projectID) == body["projectID"]
                state = conn.execute(
                    "SELECT p.preparation_state, p.default_agent_id, q.agent_state, "
                    "q.workspace_state, q.reason_code FROM ac_projects p "
                    "JOIN ac_project_preparations q ON q.project_id=p.project_id "
                    "WHERE p.project_id=?",
                    (str(receipt.projectID),),
                ).fetchone()
                assert state is not None
                assert state[0] == "failed" and state[1] is not None
                assert state[2:] == ("verified", "verified", "providerUnavailable")

            await other.acquire()
            await other.close()
            await server.start()
            await ensure_control_plane_bound(server)
            assert server.agentcomputer_http_operations.closing is False
            restarted = await client.get(f"{PREFIX}/projects/{receipt.projectID}", headers=auth)
            assert restarted.status_code == 200
            view = ProjectView.model_validate(restarted.json())
            assert view.preparationState == "failed" and view.reasonCode == "providerUnavailable"
            second = await client.post(f"{PREFIX}/projects", headers=auth, json=project_command())
            assert second.status_code == 201
    finally:
        release.set()
        if request is not None and not request.done():
            request.cancel()
            with suppress(asyncio.CancelledError):
                await request
        if stop is not None and not stop.done():
            await stop
        await server.stop()
        await other.close()


async def test_new_authenticated_request_during_and_after_pool_close_never_reads_database(
    env, monkeypatch
):
    client, server, auth = env
    assert server.services is not None
    pool = server.services.db
    entered, release = Event(), Event()
    original_close = pool.close
    original_connect = pool.connect
    reads_after_close: list[str] = []
    stop = None

    def delayed_close():
        entered.set()
        assert release.wait(timeout=10)
        original_close()

    def observed_connect():
        if entered.is_set():
            reads_after_close.append("database accessed during shutdown")
        return original_connect()

    monkeypatch.setattr(pool, "close", delayed_close)
    monkeypatch.setattr(pool, "connect", observed_connect)
    try:
        stop = asyncio.create_task(server.stop())
        assert await asyncio.to_thread(entered.wait, 10)
        for path in (
            f"{PREFIX}/capabilities",
            f"{PREFIX}/commands/{uuid4()}",
        ):
            response = await client.get(path, headers=auth)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "runtimeUnavailable"
            assert response.headers["cache-control"] == "no-store"
            assert "x-octop-access-token" not in response.headers
        assert reads_after_close == []

        release.set()
        await asyncio.wait_for(stop, timeout=10)
        closed = await client.get(f"{PREFIX}/capabilities", headers=auth)
        assert closed.status_code == 503
        assert closed.json()["error"]["code"] == "runtimeUnavailable"
        assert reads_after_close == []
    finally:
        release.set()
        if stop is not None and not stop.done():
            await stop
