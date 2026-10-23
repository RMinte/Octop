"""Task browser isolation, recording ownership and explicit recorded-skill application."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from langchain.agents.middleware import AgentMiddleware
from pydantic import Field

from octop.infra.browser.sessions import CONTROL_OWNERS, control_owner_for, resolve_browser_session
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import workbench
from octop.infra.skills.skill_packages import validate_skill_slug


class BrowserHandoff(workbench.ScopeRequest):
    target: Literal["agent", "user"]
    reason: str = Field(default="", max_length=1024)


class RecordingStart(workbench.ScopeRequest):
    name: str | None = Field(default=None, max_length=256)
    objective: str | None = Field(default=None, max_length=4096)
    privacy: Literal["mask-sensitive", "strict"] = "mask-sensitive"


class RecordingStop(workbench.ScopeRequest):
    recording_id: str | None = Field(
        default=None, alias="recordingId", pattern=r"^[A-Za-z0-9_-]{1,256}$"
    )
    name: str | None = Field(default=None, max_length=256)
    generate_steps: bool = Field(default=True, alias="generateSteps")


class RecordingApply(workbench.ScopeRequest):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    name: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=131072)


def profile_prefix(task_id: str) -> str:
    return "task-" + hashlib.sha256(task_id.encode()).hexdigest()[:16] + "-"


def public_url(value: str) -> bool:
    return not value or value == "about:blank" or urlsplit(value).scheme in {"", "http", "https"}


async def browser(scope: workbench.WorkbenchScope, *, create: bool = True) -> Any:
    return await resolve_browser_session(
        scope.browser_profile,
        create=create,
        screenshots_dir=Path(scope.view.server_directory) / "outbound" / "screenshots",
    )


async def record_request(body: dict[str, Any]) -> dict[str, Any]:
    try:
        from octop_browser.record.daemon import send_request

        value = await send_request(body)
        if not value.get("ok", True):
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
        return dict(value)
    except OctopError:
        raise
    except Exception as exc:
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE) from exc


def owned_recording(
    scope: workbench.WorkbenchScope, recording_id: str, *, current: bool = True
) -> Any:
    from octop_browser.record.store import RecordingStore

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", recording_id):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    try:
        manifest = RecordingStore().read_manifest(recording_id)
    except Exception as exc:
        raise OctopError.localized(ErrorCode.NOT_FOUND) from exc
    profile = str(getattr(manifest, "profile", ""))
    if profile != scope.browser_profile and (
        current or not profile.startswith(profile_prefix(scope.view.task_id))
    ):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return manifest


async def recording_status(scope: workbench.WorkbenchScope) -> dict[str, Any]:
    from octop_browser.record.store import RecordingStore

    try:
        data = await record_request({"command": "status"})
    except OctopError:
        data = {"ok": True, "active": None}
    active = data.get("active")
    data["active"] = (
        active
        if isinstance(active, dict) and active.get("profile") == scope.browser_profile
        else None
    )
    rows = await asyncio.to_thread(RecordingStore().list_recordings)
    rows = [row for row in rows if row.profile.startswith(profile_prefix(scope.view.task_id))]
    data["latestRecordingId"] = (
        max(rows, key=lambda row: row.created_at).recording_id if rows else None
    )
    return data


async def recording_content(scope: workbench.WorkbenchScope, identifier: str) -> dict[str, Any]:
    manifest = await asyncio.to_thread(owned_recording, scope, identifier, current=False)
    from octop_browser.record.store import RecordingStore

    path = Path(RecordingStore().recordings_dir) / identifier / "draft.skill.md"
    content = await asyncio.to_thread(path.read_text, encoding="utf-8") if path.is_file() else None
    return {
        "ok": True,
        "recordingId": identifier,
        "skillContent": content,
        "skillName": getattr(getattr(manifest, "target", None), "title", None) or identifier,
        "skillExists": content is not None,
    }


async def close_task(task_id: str) -> None:
    try:
        from octop_browser.tool_interface import _registry
    except ImportError:
        return
    import contextlib

    prefix = profile_prefix(task_id)
    for profile, session in list(_registry.items()):
        if profile.startswith(prefix):
            _registry.pop(profile, None)
            CONTROL_OWNERS.pop(profile, None)
            with contextlib.suppress(Exception):
                await session.close()


async def start_recording(scope: workbench.WorkbenchScope, body: RecordingStart) -> dict[str, Any]:
    from octop_browser.record.daemon import ensure_daemon

    await browser(scope)
    daemon = await ensure_daemon()
    if not daemon.get("ok"):
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    return await record_request(
        {
            "command": "start",
            "profile": scope.browser_profile,
            "name": body.name,
            "privacy": "mask-all" if body.privacy == "strict" else body.privacy,
            "screenshots": "off",
        }
    )


async def stop_recording(scope: workbench.WorkbenchScope, body: RecordingStop) -> dict[str, Any]:
    identifier = body.recording_id
    if identifier is None:
        data = await recording_status(scope)
        identifier = (data.get("active") or {}).get("recordingId") or (
            data.get("active") or {}
        ).get("recording_id")
    if not identifier:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    await asyncio.to_thread(owned_recording, scope, identifier)
    data = await record_request(
        {
            "command": "stop",
            "recording_id": identifier,
            "generate_steps": body.generate_steps,
            "name": body.name,
        }
    )
    await asyncio.to_thread(owned_recording, scope, identifier)
    from octop_browser.record.recorder import RecordController
    from octop_browser.record.store import RecordingStore

    store = RecordingStore()
    if body.generate_steps:
        await asyncio.to_thread(RecordController(store=store).generate_skill, identifier, body.name)
    path = Path(store.recordings_dir) / identifier / "draft.skill.md"
    content = await asyncio.to_thread(path.read_text, encoding="utf-8") if path.is_file() else None
    return {
        **data,
        "recordingId": identifier,
        "skillContent": content,
        "skillName": body.name or identifier,
    }


def _persist(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


async def apply_recording(
    scope: workbench.WorkbenchScope, identifier: str, body: RecordingApply
) -> dict[str, Any]:
    async with workbench.scope_lock("recording", str(scope.runtime_dir)):
        return await _apply_recording(scope, identifier, body)


async def _apply_recording(
    scope: workbench.WorkbenchScope, identifier: str, body: RecordingApply
) -> dict[str, Any]:
    await asyncio.to_thread(owned_recording, scope, identifier, current=False)
    try:
        slug = validate_skill_slug(body.name)
    except ValueError as exc:
        raise OctopError.localized(ErrorCode.PROJECT_RESOURCE_INVALID) from exc
    target = await workbench.public_path(scope, f"skills/{slug}/SKILL.md", mutable=True)
    fingerprint = hashlib.sha256(
        json.dumps([identifier, slug, body.content, scope.view.scope_revision]).encode()
    ).hexdigest()
    receipts = scope.runtime_dir / "workbench" / "recording-requests"
    receipt = receipts / f"{hashlib.sha256(body.request_id.encode()).hexdigest()}.json"
    if receipt.exists():
        previous = json.loads(await asyncio.to_thread(receipt.read_text))
        if previous["fingerprint"] != fingerprint:
            raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
        return dict(previous["result"])
    existing = await workbench.workspace_io(scope.workspace.adownload_bytes(target))
    if existing is not None and existing != body.content.encode():
        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
    await workbench.workspace_io(scope.workspace.aupload_bytes(target, body.content.encode()))
    manifest = scope.runtime_dir / "workbench" / "skills.json"
    entries = json.loads(await asyncio.to_thread(manifest.read_text)) if manifest.exists() else {}
    entries[slug] = {
        "path": target,
        "directory": scope.view.server_directory,
        "recording_id": identifier,
    }
    await asyncio.to_thread(_persist, manifest, entries)
    result = {
        "task_id": scope.view.task_id,
        "path": target,
        "name": slug,
        "recordingId": identifier,
    }
    await asyncio.to_thread(_persist, receipt, {"fingerprint": fingerprint, "result": result})
    return result


async def task_skill_files(
    scope: workbench.WorkbenchScope,
) -> list[tuple[str, list[tuple[str, bytes]]]]:
    manifest = scope.runtime_dir / "workbench" / "skills.json"
    if not manifest.exists():
        return []
    entries = json.loads(await asyncio.to_thread(manifest.read_text))
    files = []
    for slug, binding in entries.items():
        if binding["directory"] != scope.view.server_directory:
            continue
        data = await scope.workspace.adownload_bytes(workbench.path_in_workspace(binding["path"]))
        if data is None:
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
        files.append((slug, [("SKILL.md", data)]))
    return files


class TaskBrowserMiddleware(AgentMiddleware[Any, Any]):
    def __init__(
        self, services: SharedServices, owner_id: int, task_id: str, revision: str
    ) -> None:
        self.services, self.owner_id, self.task_id, self.revision = (
            services,
            owner_id,
            task_id,
            revision,
        )

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        if request.tool_call["name"] != "browser_use":
            return await handler(request)
        scope = await asyncio.to_thread(
            workbench.resolve,
            self.services,
            self.owner_id,
            self.task_id,
            expected_scope_revision=self.revision,
            mutable=True,
            permission="browser",
        )
        if control_owner_for(scope.browser_profile) == "user":
            raise OctopError.localized(ErrorCode.TASK_BUSY)
        args = dict(request.tool_call.get("args") or {})
        urls = [args.get("url"), *(args.get("urls") or [])]
        if any(not public_url(str(url)) for url in urls if url):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        args["profile"] = scope.browser_profile
        await browser(scope)
        return await handler(request.override(tool_call={**request.tool_call, "args": args}))
