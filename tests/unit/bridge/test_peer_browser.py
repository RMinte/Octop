"""Bridge browser requests must honor the owner's current account status."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from octop.infra.bridge.manager import BridgeManager
from octop.infra.bridge.transport import BridgeSession
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_new_browser_request_checks_current_owner_status(
    tmp_path: Path, disabled: bool
) -> None:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    users = UserRepo(pool)
    owner_id = users.create(username="owner", password_hash="x", role="admin")
    repo = MagicMock()
    repo.get.return_value = SimpleNamespace(owner_user_id=owner_id)
    manager = BridgeManager(
        bridge_repo=repo,
        secret_repo=MagicMock(),
        user_repo=users,
        advertise_base_url="http://local.test",
    )
    send_text = AsyncMock()
    session = BridgeSession(connection_id="connection", send_text=send_text)
    manager._sessions["connection"] = session
    runner = AsyncMock()
    manager.bind_asgi_app(SimpleNamespace(), peer_browser_runner=runner)
    try:
        # The Bridge is already connected when an administrator revokes the
        # owner. A new browser.start must check the database, not socket auth.
        if disabled:
            users.set_disabled(owner_id, True)
        await manager._execute_peer_browser(
            "connection",
            {"request_id": "new-browser", "start": {"type": "start"}},
        )
        frames = [json.loads(call.args[0]) for call in send_text.await_args_list]
        if disabled:
            runner.assert_not_awaited()
            assert [frame["type"] for frame in frames] == ["browser.error", "browser.end"]
        else:
            runner.assert_awaited_once()
            assert runner.await_args.kwargs["user_id"] == owner_id
            assert [frame["type"] for frame in frames] == ["browser.end"]
        assert not manager._browser_peer_clients
    finally:
        await session.close()
        pool.close()
