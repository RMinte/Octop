"""JWT database lock waits must leave the event loop free to do other work."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI

from octop.api.deps import ACCESS_TOKEN_RESPONSE_HEADER, current_user, sign_token
from octop.api.middleware.jwt_auth import install
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.server import OctopServer
from octop.infra.users.identity import User


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["authenticate", "renew", "dependency"])
async def test_jwt_database_contention_allows_event_loop_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    pool = SqlitePool(tmp_path / "jwt.db")
    run_migrations(pool)
    secrets = SecretRepo(pool)
    secret = secrets.get_or_create("jwt", lambda: b"jwt-contention-test-secret" * 2)
    user = User(id=7, username="owner", role="user", display_name=None)
    server = OctopServer(home=tmp_path)
    server.services = SimpleNamespace(
        secret_repo=secrets,
        config=SimpleNamespace(access_token_ttl_seconds=3600),
    )
    server.app_runtime = SimpleNamespace(
        user_manager=SimpleNamespace(get_by_id=lambda user_id: user if user_id == user.id else None)
    )
    server._started = True
    token = sign_token(secret, sub=user.id, uname=user.username, role=user.role, ttl_seconds=60)
    loop = asyncio.get_running_loop()
    locked = asyncio.Event()
    lookup_started = asyncio.Event()
    lock_held = threading.Event()
    release = threading.Event()
    holder: asyncio.Task[bool] | None = None
    request: asyncio.Task[httpx.Response] | None = None
    original_get = secrets.get

    def observed_get(key: str) -> bytes | None:
        if lock_held.is_set():
            loop.call_soon_threadsafe(lookup_started.set)
        return original_get(key)

    monkeypatch.setattr(secrets, "get", observed_get)

    def hold_database() -> bool:
        with pool.transaction():
            lock_held.set()
            loop.call_soon_threadsafe(locked.set)
            # Watchdog only: correctness is whether the async test releases
            # the lock while this transaction is held, not an elapsed duration.
            released_by_event_loop = release.wait(5)
            lock_held.clear()
        return released_by_event_loop

    async def acquire_database() -> None:
        nonlocal holder
        holder = asyncio.create_task(asyncio.to_thread(hold_database))
        await asyncio.wait_for(locked.wait(), timeout=10)

    app = FastAPI()
    app.state.octop_server = server
    if phase != "dependency":
        install(app, server)

    @app.get("/api/private")
    async def private(authenticated: User = Depends(current_user)) -> dict[str, int]:  # noqa: B008
        if phase == "renew":
            await acquire_database()
        return {"user_id": authenticated.id}

    try:
        if phase != "renew":
            await acquire_database()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            request = asyncio.create_task(
                client.get("/api/private", headers={"Authorization": f"Bearer {token}"})
            )
            # This event can run only if a contended JWT lookup yields the
            # event loop. A synchronous lookup makes the watchdog release it.
            await asyncio.wait_for(lookup_started.wait(), timeout=10)
            release.set()
            response = await request
        assert holder is not None
        assert await holder, "JWT lookup blocked event-loop progress until the watchdog expired"
        assert response.status_code == 200
        assert response.json() == {"user_id": user.id}
        if phase != "dependency":
            assert response.headers.get(ACCESS_TOKEN_RESPONSE_HEADER)
    finally:
        release.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        if holder is not None:
            await holder
        pool.close()
