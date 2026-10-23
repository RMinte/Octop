"""Helpers for attaching dashboard / chat UI to octop-browser sessions."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from octop.api.deps import current_user
from octop.infra.browser.sessions import CONTROL_OWNERS as _CONTROL_OWNERS
from octop.infra.browser.sessions import control_owner_for as control_owner_for
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.browser_media import user_browser_profile

logger = logging.getLogger(__name__)

router = APIRouter()


# In-memory control ownership per session (keyed by harness profile / session_id).
# This is a UI/coordination hint: the agent pauses interactive input while the
# user is in control. It is stored separately from the live session so a
# takeover survives dashboard reloads and WS reconnects.
class HandoffBody(BaseModel):
    """Control handoff request: switch between agent and user control."""

    target: str  # "agent" | "user"
    reason: str = ""


async def resolve_harness_session(
    profile_hint: str | None,
    *,
    server: Any | None = None,
    agent_id: str | None = None,
    create: bool = True,
    screenshots_dir: Path | None = None,
) -> Any | None:
    from octop.infra.browser.sessions import resolve_browser_session

    if screenshots_dir is None and server is not None and agent_id:
        from octop.api.common.agent_workspace import resolve_agent_workspace_dir
        from octop.infra.utils.browser_media import agent_outbound_screenshots_dir

        screenshots_dir = agent_outbound_screenshots_dir(
            resolve_agent_workspace_dir(server, agent_id)
        )
    return await resolve_browser_session(
        profile_hint, create=create, screenshots_dir=screenshots_dir
    )


async def harness_page_url(sess: Any) -> str:
    try:
        client = sess._internal.client  # noqa: SLF001
        send = getattr(client, "send_passive", client.send)
        info = await send(
            "Runtime.evaluate",
            {
                "expression": "location.href",
                "returnByValue": True,
            },
        )
        return str(info.get("result", {}).get("value", "") or "")
    except Exception:
        return ""


async def harness_list_tabs(sess: Any) -> list[dict[str, Any]]:
    """List open page targets in a harness session (CDP /json)."""
    import aiohttp

    host = sess._internal._cfg.cdp_host  # noqa: SLF001
    port = sess._internal._profile.cdp_port  # noqa: SLF001
    try:
        timeout = aiohttp.ClientTimeout(total=3)
        async with (
            aiohttp.ClientSession() as http,
            http.get(f"http://{host}:{port}/json", timeout=timeout) as resp,
        ):
            targets = await resp.json(content_type=None)
    except Exception as exc:
        logger.debug("harness_list_tabs failed: %s", exc)
        return []

    pages = [t for t in targets if t.get("type") == "page"]
    current_target_id = None
    try:
        current_target_id = sess._internal._profile.load_target()  # noqa: SLF001
    except Exception:
        current_target_id = None
    tabs: list[dict[str, Any]] = []
    for i, t in enumerate(pages):
        url = str(t.get("url", "") or "")
        tabs.append(
            {
                "id": t.get("id", i),
                "idx": i,
                "url": url,
                "title": str(t.get("title", "") or ""),
                "active": bool(current_target_id and t.get("id") == current_target_id),
            }
        )
    if tabs and not any(t["active"] for t in tabs):
        tabs[0]["active"] = True
    return tabs


async def harness_sessions_payload(profile_name: str) -> dict[str, Any]:
    """Shape expected by the dashboard ``BrowserSessionsResponse`` type."""
    try:
        from octop_browser.tool_interface import _registry
    except ImportError:
        return {"ok": False, "environment": "headless-server", "sessions": []}

    now = int(time.time() * 1000)
    sessions: list[dict[str, Any]] = []
    for profile, sess in list(_registry.items()):
        if profile != profile_name:
            continue
        url = await harness_page_url(sess)
        sessions.append(
            {
                "session_id": profile,
                "profile_name": profile,
                "conversation_id": profile,
                "channel_source": "dashboard",
                "state": "streaming" if url else "idle",
                "control_owner": control_owner_for(profile),
                "current_url": url,
                "created_at": now,
                "last_activity_at": now,
            }
        )
    return {
        "ok": True,
        "environment": "headless-server",
        "sessions": sessions,
    }


@router.get("/browser/harness-sessions")
async def list_harness_sessions(user: Any = Depends(current_user)) -> dict[str, Any]:
    """List the current user's live octop-browser profile."""
    return await harness_sessions_payload(user_browser_profile(user.id))


@router.post("/browser/sessions/{session_id}/handoff")
async def handoff(
    session_id: str,
    body: HandoffBody,
    user: Any = Depends(current_user),
) -> dict[str, Any]:
    """Switch control of a browser session between the agent and the user.

    The control owner is a coordination hint (the agent yields interactive
    input to the user while they are in control). It is stored in-memory per
    session and reflected in ``harness-sessions`` and the WS screencast so a
    takeover survives dashboard reloads and reconnects.
    """
    profile = user_browser_profile(user.id)
    if session_id not in {"", "auto"} and session_id != profile:
        logger.debug("handoff path %r ignored; using %r", session_id, profile)
    target = body.target
    if target not in ("agent", "user"):
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, "target must be 'agent' or 'user'")
    _CONTROL_OWNERS[profile] = target

    payload = await harness_sessions_payload(profile)
    session = next(
        (s for s in payload.get("sessions", []) if s["session_id"] == profile),
        None,
    )
    if session is None:
        # Session not currently live — return a minimal payload so the UI can
        # still flip its local control-owner state.
        now = int(time.time() * 1000)
        session = {
            "session_id": profile,
            "profile_name": profile,
            "conversation_id": profile,
            "channel_source": "dashboard",
            "state": "idle",
            "control_owner": target,
            "current_url": "",
            "created_at": now,
            "last_activity_at": now,
        }
    return {"ok": True, "session": session}


@router.post(
    "/browser/shutdown",
    summary="Stop the local Chrome process for an octop-browser profile",
)
async def shutdown_browser(user: Any = Depends(current_user)) -> dict[str, Any]:
    """Terminate the current user's Octop-managed Chrome. Cookies stay on disk."""
    try:
        from octop_browser.tool_interface import browser_tool
    except ImportError as exc:
        raise OctopError(
            ErrorCode.INTERNAL_ERROR,
            "octop-browser not installed",
            status=503,
        ) from exc

    hint = user_browser_profile(user.id)

    result = await browser_tool(action="close_session", profile=hint, kill=True)
    _CONTROL_OWNERS.pop(hint, None)
    if not result.success:
        raise OctopError(
            ErrorCode.INTERNAL_ERROR,
            result.error or f"failed to stop browser profile {hint!r}",
            status=503,
        )
    return {"ok": True, "profile": hint}
