"""Foreground composition keeps daemon ownership through every cleanup path."""

from __future__ import annotations

import pytest

from octop import launch
from octop.infra.agentcomputer.bootstrap import DaemonBootstrap
from octop.infra.server import OctopServer


@pytest.mark.parametrize("planning_fails", [False, True])
async def test_foreground_constructs_daemon_and_cleans_on_serve_or_planning_exit(
    tmp_octop_home, monkeypatch, planning_fails
):
    constructions = []
    instances = []

    def construct(**kwargs):
        constructions.append(kwargs)
        server = OctopServer(home=tmp_octop_home, **kwargs)
        instances.append(server)
        return server

    async def serve(_server):
        server = instances[0]
        assert server._agentcomputer_bootstrap is not None
        other = DaemonBootstrap(server.paths.agentcomputer_identity.parent)
        from octop.infra.errors import OctopError

        with pytest.raises(OctopError):
            await other.acquire()

    monkeypatch.setattr(launch, "OctopServer", construct)
    monkeypatch.setattr(launch, "_schedule_linux_bubblewrap_ensure", lambda: None)
    monkeypatch.setattr(launch, "_serve", serve)
    if planning_fails:

        def fail(*args, **kwargs):
            raise RuntimeError("fixture listen planning failure")

        monkeypatch.setattr("octop.infra.setup.tls.listeners.build_listen_plan", fail)
    kwargs = {
        "host": "127.0.0.1",
        "port": 8099,
        "reload": False,
        "workers": 1,
        "log_level": "warning",
        "ssl_certfile": None,
        "ssl_keyfile": None,
    }
    if planning_fails:
        with pytest.raises(RuntimeError, match="listen planning"):
            await launch.run_foreground(**kwargs)
    else:
        await launch.run_foreground(**kwargs)
    assert len(constructions) == 1
    server = instances[0]
    assert constructions[0] == {
        "daemon_bootstrap": server._agentcomputer_bootstrap,
    }
    assert server.services is None and server.app_runtime is None
    other = DaemonBootstrap(server.paths.agentcomputer_identity.parent)
    await other.acquire()
    await other.close()
