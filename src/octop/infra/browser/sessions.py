"""Browser session lifecycle shared by transport adapters and task tools."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from octop.infra.errors import ErrorCode, OctopError

logger = logging.getLogger(__name__)
CONTROL_OWNERS: dict[str, str] = {}


def control_owner_for(session_id: str) -> str:
    return CONTROL_OWNERS.get(session_id, "agent")


async def _is_session_alive(sess: Any, *, timeout: float = 2.0) -> bool:
    """Probe a cached session with a cheap CDP round-trip.

    A previously-registered session's underlying CDP WebSocket can die out
    from under us (browser crash, OOM kill, network blip) while the Python
    object stays in ``_registry`` forever. Reusing a dead session makes every
    subsequent action fail with confusing low-level errors like
    ``no close frame received or sent``. This does a fast, side-effect-free
    ``Runtime.evaluate`` to confirm the CDP connection is still usable.
    """
    try:
        client = sess._internal.client  # noqa: SLF001
        send = getattr(client, "send_passive", client.send)
        await asyncio.wait_for(
            send("Runtime.evaluate", {"expression": "1", "returnByValue": True}),
            timeout=timeout,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("cached harness session failed health check: %s", exc)
        return False


async def resolve_browser_session(
    profile_hint: str | None,
    *,
    screenshots_dir: Path | None = None,
    create: bool = True,
) -> Any | None:
    """Return a live :class:`octop_browser.BrowserSession` for ``profile_hint``.

    *profile_hint* must be a concrete profile name (``user-<id>``). Empty /
    ``auto`` never falls back to another user's session or to ``default``.

    When ``create=True`` (screencast / interactive clients) a missing live
    session is launched. Listen-only dashboard hooks pass ``create=False``
    so a status WebSocket cannot spawn Chrome just to watch for updates.

    Cached entries are health-checked before reuse — a dead/stale session
    (e.g. browser crashed) is evicted and replaced with a freshly launched
    one rather than being handed back to fail again.

    When ``create=False`` and no live session exists, returns ``None``.
    """
    try:
        from octop_browser import BrowserSession
        from octop_browser.tool_interface import _registry
    except ImportError as exc:
        raise OctopError(
            ErrorCode.INTERNAL_ERROR,
            "octop-browser not installed",
            status=503,
        ) from exc

    hint = (profile_hint or "").strip()
    if not hint or hint == "auto":
        if not create:
            return None
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, "browser profile is required")

    cached = _registry.get(hint)
    if cached is not None:
        if await _is_session_alive(cached):
            return cached
        logger.warning(
            "harness session %r is dead (stale CDP connection); discarding%s",
            hint,
            " and relaunching" if create else "",
        )
        _registry.pop(hint, None)
        with contextlib.suppress(Exception):
            await cached.close()

    if not create:
        return None

    profile = hint
    from octop.infra.utils.browser_media import harness_settings_for_screenshots_dir

    harness_settings = (
        harness_settings_for_screenshots_dir(screenshots_dir)
        if screenshots_dir is not None
        else None
    )

    from octop.infra.browser.setup import (  # noqa: PLC0415
        prepare_harness_profile_for_launch,
        resolve_browser_display,
    )

    # Shared ~/.octop/browser-profiles (not per-agent workspace).
    await prepare_harness_profile_for_launch(profile)
    # Virtual desktop (Xvnc :99) → headed Chrome so the window shows on
    # remote desktop; otherwise force headless (do not use mode=auto — a
    # stale $DISPLAY would still resolve to headed and crash Chromium).
    display = resolve_browser_display()
    launch_mode = "headed" if display else "headless"

    # Fresh ProfileManager picks up any BROWSER_USE_PROFILES_DIR relocation
    # done by prepare (default singleton is bound at import time).
    from octop_browser.profile import ProfileManager  # noqa: PLC0415
    from octop_browser.settings import settings as hb_settings  # noqa: PLC0415

    profile_manager = ProfileManager(base_dir=Path(hb_settings.profiles_dir))

    try:
        sess = await BrowserSession.create(
            profile=profile,
            mode=launch_mode,  # type: ignore[arg-type]
            settings=harness_settings,
            profile_manager=profile_manager,
        )
    except Exception as exc:
        # Chrome exit 21 / ProcessSingleton usually means a stale lock or a
        # non-writable profile left by a previous root/non-root mismatch.
        msg = str(exc)
        if (
            "returncode=21" in msg
            or "ProcessSingleton" in msg
            or "SingletonLock" in msg
            or "profile directory" in msg.lower()
            or "/run/user/" in msg
        ):
            logger.warning(
                "Browser launch failed for %r (%s); recovering profile and retrying",
                profile,
                exc,
            )
            await prepare_harness_profile_for_launch(profile, force_recover=True)
            display = resolve_browser_display()
            launch_mode = "headed" if display else "headless"
            profile_manager = ProfileManager(base_dir=Path(hb_settings.profiles_dir))
            try:
                sess = await BrowserSession.create(
                    profile=profile,
                    mode=launch_mode,  # type: ignore[arg-type]
                    settings=harness_settings,
                    profile_manager=profile_manager,
                )
            except Exception as retry_exc:
                raise OctopError(
                    ErrorCode.INTERNAL_ERROR,
                    f"failed to attach browser profile {profile!r}: {retry_exc}",
                    status=503,
                ) from retry_exc
        else:
            raise OctopError(
                ErrorCode.INTERNAL_ERROR,
                f"failed to attach browser profile {profile!r}: {exc}",
                status=503,
            ) from exc
    _registry[profile] = sess
    return sess
