"""Resumable old-thread conversion; only the final SQL commit publishes a Task."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from octop.infra.db.repos.conversation_migrations import MigrationBatchRow, MigrationItemRow
from octop.infra.db.repos.project_task_attachments import ProjectTaskAttachmentRow
from octop.infra.db.repos.project_task_legacy_sources import LegacyConversationSourceRow
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.repos.unified_sources import UnifiedSourceRoute, UnifiedSourceRouteRepo
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.history.migration_source import SourceUnavailable, read_source
from octop.infra.history.migration_trajectory import attach_trajectory
from octop.infra.projects import approval_policy, input_attachments, source_routes
from octop.infra.projects.deletion_state import assert_project_writable
from octop.infra.projects.expert_profiles import _workspace, expert_configuration
from octop.infra.projects.legacy_conversations import _files, _local_backend, _visible_turns
from octop.infra.projects.models import ProjectModel, Reference
from octop.infra.projects.tasks import RequestID, now_ms
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)


class MigrationCreate(ProjectModel):
    request_id: RequestID
    thread_ids: list[Reference] = Field(
        default_factory=list,
        max_length=10000,
        description="空列表表示本人全部普通来源；私人管家和原型单列报告。",
    )
    project_id: Reference | None = Field(
        default=None, description="明确映射到本人项目；省略或null使用无项目独立目录，不推断旧cwd。"
    )


class MigrationCandidate(ProjectModel):
    thread_id: str
    agent_id: str
    channel_type: str
    disposition: str


class MigrationPreflight(ProjectModel):
    candidates: list[MigrationCandidate]
    total: int
    eligible: int
    requires_model: bool = False


class MigrationItem(ProjectModel):
    item_id: str
    source_agent_id: str
    source_thread_id: str
    target_task_id: str
    status: str
    phase: str
    error_code: str
    source_digest: str
    manifest: dict[str, Any]


class MigrationBatch(ProjectModel):
    batch_id: str
    status: str
    created_at: int
    updated_at: int
    items: list[MigrationItem]


class MigrationControl(ProjectModel):
    action: Literal["pause", "resume"]


class MigrationAutomation(ProjectModel):
    enabled: bool


class LegacyTaskLink(ProjectModel):
    thread_id: str
    task_id: str | None = None
    state: str
    url: str | None = None


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _owner(services: SharedServices, owner_id: int) -> None:
    user = services.user_repo.get(owner_id)
    if user is None or user.disabled:
        raise OctopError.localized(ErrorCode.NOT_FOUND)


def _candidate(services: SharedServices, thread: Any) -> MigrationCandidate:
    disposition = "eligible"
    if services.thread_repo.is_resident(thread.thread_id):
        disposition = "resident_adapter_required"
    elif thread.channel_type.startswith(("agentcomputer", "ac_")) or thread.session_key.startswith(
        ("agentcomputer:", "ac:")
    ):
        disposition = "prototype_archive_required"
    else:
        agent = services.agent_repo.get(thread.agent_id)
        if agent is None:
            disposition = "source_agent_missing"
        else:
            try:
                from octop.infra.backend.resolver import resolve_agent_backend_spec

                config = json.loads(agent.config_json or "{}")
                spec = resolve_agent_backend_spec(
                    config.get("backend"), repo=services.storage_backend_repo
                )
                if not _local_backend(spec):
                    disposition = "remote_adapter_required"
            except (ValueError, TypeError, KeyError):
                disposition = "source_configuration_invalid"
    return MigrationCandidate(
        thread_id=thread.thread_id,
        agent_id=thread.agent_id,
        channel_type=thread.channel_type,
        disposition=disposition,
    )


def preflight(services: SharedServices, owner_id: int, body: MigrationCreate) -> MigrationPreflight:
    _owner(services, owner_id)
    threads = services.conversation_migration_repo.threads(owner_id, body.thread_ids)
    if body.thread_ids and {row.thread_id for row in threads} != set(body.thread_ids):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    if body.project_id:
        project = services.project_repo.get(body.project_id, owner_id)
        if project is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if project.archived:
            raise OctopError.localized(ErrorCode.PROJECT_ARCHIVED)
    candidates = [_candidate(services, thread) for thread in threads]
    return MigrationPreflight(
        candidates=candidates,
        total=len(candidates),
        eligible=sum(row.disposition == "eligible" for row in candidates),
    )


def create(services: SharedServices, owner_id: int, body: MigrationCreate) -> MigrationBatch:
    preview = preflight(services, owner_id, body)
    repo = services.conversation_migration_repo
    fingerprint, now = _hash(body.model_dump(mode="json")), now_ms()
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        previous = repo.by_request(conn, owner_id, body.request_id)
        if previous is not None:
            if previous.fingerprint != fingerprint:
                raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
            batch_id = previous.batch_id
        else:
            batch_id = "migration_" + new_ulid()
            repo.insert_batch(
                conn,
                MigrationBatchRow(
                    batch_id,
                    owner_id,
                    body.request_id,
                    fingerprint,
                    body.model_dump_json(),
                    "queued" if preview.eligible else "completed",
                    now,
                    now,
                ),
            )
            for source in preview.candidates:
                repo.insert_item(
                    conn,
                    MigrationItemRow(
                        "migration_item_" + new_ulid(),
                        batch_id,
                        owner_id,
                        source.agent_id,
                        source.thread_id,
                        "",
                        "task_" + new_ulid(),
                        "pending" if source.disposition == "eligible" else "skipped",
                        "discovered",
                        "",
                        "{}",
                        "" if source.disposition == "eligible" else source.disposition,
                        "",
                        0,
                        now,
                        now,
                    ),
                )
    return detail(services, owner_id, batch_id)


def detail(services: SharedServices, owner_id: int, batch_id: str) -> MigrationBatch:
    _owner(services, owner_id)
    batch = services.conversation_migration_repo.get(owner_id, batch_id)
    if batch is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return MigrationBatch(
        batch_id=batch.batch_id,
        status=batch.status,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        items=[
            MigrationItem(
                **{
                    key: getattr(item, key)
                    for key in (
                        "item_id",
                        "source_agent_id",
                        "source_thread_id",
                        "target_task_id",
                        "status",
                        "phase",
                        "error_code",
                        "source_digest",
                    )
                },
                manifest=json.loads(item.manifest_json),
            )
            for item in services.conversation_migration_repo.items(owner_id, batch_id)
        ],
    )


def batches(services: SharedServices, owner_id: int) -> list[MigrationBatch]:
    _owner(services, owner_id)
    return [
        detail(services, owner_id, row.batch_id)
        for row in services.conversation_migration_repo.batches(owner_id)
    ]


def control(services: SharedServices, owner_id: int, batch_id: str, action: str) -> MigrationBatch:
    detail(services, owner_id, batch_id)
    services.conversation_migration_repo.control(owner_id, batch_id, action, now_ms())
    return detail(services, owner_id, batch_id)


def automation(
    services: SharedServices, owner_id: int, enabled: bool | None = None
) -> MigrationAutomation:
    _owner(services, owner_id)
    key = f"conversation_migration_automatic:{owner_id}"
    if enabled is not None:
        services.settings_repo.set(key, "enabled" if enabled else "disabled")
    return MigrationAutomation(enabled=services.settings_repo.get(key) == "enabled")


def legacy_link(services: SharedServices, owner_id: int, thread_id: str) -> LegacyTaskLink:
    thread = services.thread_repo.get(thread_id)
    if thread is None or thread.user_id != owner_id or services.thread_repo.is_resident(thread_id):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    route = source_routes.legacy_route(services, owner_id, thread_id, thread.agent_id)
    target = services.project_task_repo.get(route.task_id, owner_id) if route.task_id else None
    state = (
        "target_deleted"
        if route.state == "task" and route.revision > 1 and target is None
        else route.state
    )
    return LegacyTaskLink(
        thread_id=thread_id,
        task_id=target.task_id if target else None,
        state=state,
        url=f"/conversations/{target.task_id}" if target else None,
    )


def _persist(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


class ConversationMigrationWorker:
    def __init__(
        self,
        services: SharedServices,
        *,
        registry: Any = None,
        hitl_store: Any = None,
        history_archive: Any = None,
    ) -> None:
        self.services, self.registry, self.hitl_store = services, registry, hitl_store
        # Archive reads use an independent read-only snapshot; this instance is
        # intentionally never initialized, repaired or written by migration.
        self.history_archive = history_archive
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self.closing = False
        self._automatic_at = 0.0

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def wake(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self.closing = True
        self.wake()
        if self._task is not None:
            await self._task

    async def _loop(self) -> None:
        while not self.closing:
            self._wake.clear()
            try:
                if time.monotonic() >= self._automatic_at:
                    await asyncio.to_thread(self.enqueue_automatic)
                    self._automatic_at = time.monotonic() + 30
                if await self.run_once():
                    continue
            except Exception:
                logger.exception("Conversation migration worker iteration failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 2)

    def enqueue_automatic(self) -> list[str]:
        queued = []
        for owner_id in self.services.conversation_migration_repo.owners():
            if not automation(self.services, owner_id).enabled:
                continue
            threads = self.services.conversation_migration_repo.threads(owner_id, [])
            ordinary = [
                thread.thread_id
                for thread in threads
                if _candidate(self.services, thread).disposition == "eligible"
            ]
            if ordinary:
                body = MigrationCreate(
                    request_id="automatic_" + _hash(sorted(ordinary)), thread_ids=ordinary
                )
                queued.append(create(self.services, owner_id, body).batch_id)
        return queued

    async def run_once(self, batch_id: str | None = None) -> bool:
        repo = self.services.conversation_migration_repo
        claimed = await asyncio.to_thread(repo.claim, new_ulid(), now_ms(), batch_id)
        if claimed is None:
            return False
        item: MigrationItemRow = claimed
        manifest: dict[str, Any] = {}
        route: UnifiedSourceRoute | None = None
        fence = None
        acquired = False
        try:
            batch = await asyncio.to_thread(repo.get, item.owner_id, item.batch_id)
            if batch is None:
                raise SourceUnavailable("batch_missing")
            body = MigrationCreate.model_validate_json(batch.configuration_json)
            thread = await asyncio.to_thread(self.services.thread_repo.get, item.source_thread_id)
            if (
                thread is None
                or thread.user_id != item.owner_id
                or thread.agent_id != item.source_agent_id
            ):
                raise SourceUnavailable("source_owner_mismatch")
            disposition = await asyncio.to_thread(_candidate, self.services, thread)
            if disposition.disposition != "eligible":
                raise SourceUnavailable(disposition.disposition)
            route = await asyncio.to_thread(
                source_routes.legacy_route,
                self.services,
                item.owner_id,
                thread.thread_id,
                thread.agent_id,
            )
            if route.state == "task":
                target = (
                    await asyncio.to_thread(
                        self.services.project_task_repo.get, route.task_id, item.owner_id
                    )
                    if route.task_id
                    else None
                )
                if target is None:
                    raise SourceUnavailable("target_deleted")
                item = replace(item, target_task_id=target.task_id)
                manifest = {"reused_task_id": target.task_id, "source_digest": route.source_digest}
                await asyncio.to_thread(self._finish, item, "completed", "", manifest)
                return True
            route, item = await asyncio.to_thread(self._drain, route, item)
            assert route is not None
            fence = source_routes.writer_fence(self.services, thread.thread_id)
            try:
                await asyncio.to_thread(fence.__enter__)
            except OctopError as exc:
                raise SourceUnavailable("source_active") from exc
            acquired = True
            if self.registry is not None and self.registry.is_agent_active(thread.agent_id):
                raise SourceUnavailable("source_active")
            if self.hitl_store is not None and self.hitl_store.resolve_pending_for_thread(
                thread.thread_id, agent_id=thread.agent_id, user_id=item.owner_id
            ):
                raise SourceUnavailable("source_active")
            agent = await asyncio.to_thread(self.services.agent_repo.get, thread.agent_id)
            if agent is None:
                raise SourceUnavailable("source_agent_missing")
            config = json.loads(agent.config_json or "{}")
            workspace = await asyncio.to_thread(_workspace, self.services, agent, config)
            snapshot = await read_source(self.services, thread, config, workspace)
            visible = _visible_turns(
                snapshot.wires, thread.created_at * 1000, allow_incomplete=True
            )
            trajectory, trajectory_warnings = attach_trajectory(visible, snapshot.trajectory)
            visible[-1].files.extend(reference.as_ref() for reference in thread.artifacts)
            manifest = {
                "version": 1,
                "source_digest": snapshot.digest,
                "anchor": snapshot.anchor,
                "warnings": [*snapshot.warnings, *trajectory_warnings],
                "trajectory_event_count": len(snapshot.trajectory),
                "turns": [],
                "model_calls": 0,
            }
            stage = (
                self.services.paths.root / "conversation-migrations" / item.batch_id / item.item_id
            )
            for index, turn in enumerate(visible):
                files = await self._stage_files(item, turn.files, stage)
                manifest["turns"].append(
                    {
                        "turn_id": "turn_" + _hash([item.target_task_id, index])[:26],
                        "prompt": turn.prompt,
                        "reply": "\n\n".join(turn.replies),
                        "status": turn.status,
                        "created_at": turn.created_at,
                        "message_ids": turn.message_ids,
                        "trajectory": trajectory[index],
                        "blocks": [block.model_dump(mode="json") for block in turn.blocks],
                        "files": files,
                    }
                )
            # Staged data is an integrity-checked receipt; it is never a public Task.
            await asyncio.to_thread(
                _persist, stage / "manifest.json", json.dumps(manifest, ensure_ascii=False).encode()
            )
            if not await asyncio.to_thread(
                repo.progress,
                item,
                phase="verified",
                digest=snapshot.digest,
                manifest={
                    "turn_count": len(visible),
                    "attachment_count": sum(len(turn["files"]) for turn in manifest["turns"]),
                    "source_digest": snapshot.digest,
                },
                now=now_ms(),
            ):
                raise SourceUnavailable("migration_paused")
            check = await read_source(self.services, thread, config, workspace)
            if check.digest != snapshot.digest:
                raise SourceUnavailable("source_changed")
            await asyncio.to_thread(
                self._publish, item, body, thread, agent, route, manifest, stage
            )
        except Exception as exc:
            code = str(exc) if isinstance(exc, SourceUnavailable) else "source_unavailable"
            await asyncio.to_thread(
                self._finish,
                item,
                "blocked" if code in {"source_active", "migration_paused"} else "failed",
                code,
                {"source_digest": manifest.get("source_digest", ""), "model_calls": 0},
                release_route=route if code != "source_active" else None,
            )
        finally:
            if acquired and fence is not None:
                await asyncio.to_thread(fence.__exit__, None, None, None)
        return True

    def _drain(
        self, route: UnifiedSourceRoute, item: MigrationItemRow
    ) -> tuple[UnifiedSourceRoute, MigrationItemRow]:
        with self.services.db.transaction() as conn:
            if not self.services.conversation_migration_repo.locked(conn, item, now_ms()):
                raise SourceUnavailable("migration_paused")
            current = UnifiedSourceRouteRepo.get(conn, item.owner_id, route.route_id)
            if current is None:
                raise SourceUnavailable("route_missing")
            if current.state == "legacy":
                if not UnifiedSourceRouteRepo.transition(
                    conn,
                    item.owner_id,
                    current.route_id,
                    current.revision,
                    "legacy",
                    "draining",
                    pending_task_id=item.target_task_id,
                    now=now_ms(),
                ):
                    raise SourceUnavailable("source_changed")
                current = UnifiedSourceRouteRepo.get(conn, item.owner_id, current.route_id)
                assert current is not None
            if current.state != "draining" or current.pending_task_id != item.target_task_id:
                raise SourceUnavailable("source_mapping_conflict")
            return current, replace(item, route_id=current.route_id)

    def _finish(
        self,
        item: MigrationItemRow,
        status: str,
        code: str,
        manifest: dict[str, Any],
        *,
        release_route: UnifiedSourceRoute | None = None,
    ) -> None:
        with self.services.db.transaction() as conn:
            changed = self.services.conversation_migration_repo.finish(
                conn, item, status, code, manifest, now_ms()
            )
            if (
                changed
                and release_route is not None
                and release_route.pending_task_id == item.target_task_id
            ):
                UnifiedSourceRouteRepo.transition(
                    conn,
                    item.owner_id,
                    release_route.route_id,
                    release_route.revision,
                    "draining",
                    "legacy",
                    now=now_ms(),
                )

    async def _stage_files(
        self, item: MigrationItemRow, references: list[dict[str, str]], stage: Path
    ) -> list[dict[str, Any]]:
        result = []
        seen = set()
        for reference in references:
            key = _hash(reference)
            if key in seen:
                continue
            seen.add(key)
            source_agent = reference.get("agent_id") or item.source_agent_id
            entry: dict[str, Any] = {
                "source": {
                    key: value for key, value in reference.items() if key != "inline_data_url"
                },
                "name": reference.get("filename")
                or reference.get("name")
                or Path(
                    reference.get("workspace_path") or reference.get("path") or "attachment"
                ).name,
                "source_agent_id": source_agent,
                "status": "missing",
            }
            try:
                data: bytes | None
                declared: str | None
                if inline := reference.get("inline_data_url"):
                    header, encoded = inline.split(",", 1)
                    if (
                        ";base64" not in header
                        or len(encoded) > input_attachments.MAX_ATTACHMENT_BYTES * 4 // 3 + 8
                    ):
                        raise ValueError("invalid inline attachment")
                    data = base64.b64decode(encoded, validate=True)
                    declared = header.removeprefix("data:").split(";", 1)[0]
                else:
                    agent = await asyncio.to_thread(self.services.agent_repo.get, source_agent)
                    if agent is None or (
                        agent.user_id != item.owner_id
                        and (not agent.is_shared or not agent.enabled)
                    ):
                        entry["status"] = "forbidden"
                        result.append(entry)
                        continue
                    workspace = await asyncio.to_thread(
                        _workspace, self.services, agent, json.loads(agent.config_json or "{}")
                    )
                    links = await _files(
                        workspace,
                        source_agent,
                        [reference],
                        shared_viewer=agent.user_id != item.owner_id,
                    )
                    if not links or not links[0].available or links[0].workspace_path is None:
                        result.append(entry)
                        continue
                    data = await workspace.adownload_bytes(links[0].workspace_path)
                    declared = reference.get("media_type")
                if data is None:
                    result.append(entry)
                    continue
                if len(data) > input_attachments.max_upload_bytes(self.services):
                    entry["status"] = "oversize"
                else:
                    media_type = await asyncio.to_thread(
                        input_attachments._media_type, entry["name"], data, declared
                    )
                    digest = hashlib.sha256(data).hexdigest()
                    attachment_id = "input_" + _hash([item.target_task_id, key, digest])[:40]
                    await asyncio.to_thread(_persist, stage / (attachment_id + ".bin"), data)
                    entry.update(
                        status="copied",
                        attachment_id=attachment_id,
                        media_type=media_type,
                        sha256=digest,
                        byte_count=len(data),
                    )
            except (OSError, ValueError, OctopError, TypeError):
                entry["status"] = "unavailable"
            result.append(entry)
        return result

    def _publish(
        self,
        item: MigrationItemRow,
        body: MigrationCreate,
        thread: Any,
        agent: Any,
        route: UnifiedSourceRoute,
        manifest: dict[str, Any],
        stage: Path,
    ) -> None:
        services, now = self.services, now_ms()
        with services.db.transaction() as conn:
            if not services.project_task_repo.lock_owner(
                conn, item.owner_id
            ) or not services.conversation_migration_repo.locked(conn, item, now):
                raise SourceUnavailable("migration_paused")
            current = UnifiedSourceRouteRepo.get(conn, item.owner_id, route.route_id)
            if current is None or current.revision != route.revision or current.state != "draining":
                raise SourceUnavailable("source_changed")
            if services.conversation_migration_repo.existing_legacy_targets(
                conn, item.owner_id, thread.thread_id
            ):
                raise SourceUnavailable("existing_target_requires_review")
            if body.project_id:
                project = services.project_repo.get_in_transaction(
                    conn, body.project_id, item.owner_id
                )
                if project is None or project.archived:
                    raise SourceUnavailable("target_project_unavailable")
                try:
                    assert_project_writable(services, conn, item.owner_id, body.project_id)
                except OctopError as exc:
                    raise SourceUnavailable("target_project_unavailable") from exc
            selected = None
            try:
                expert_configuration(services, item.owner_id, agent.agent_id)
                selected = agent.agent_id
            except OctopError:
                manifest["warnings"].append("selected_expert_unavailable")
            policy = approval_policy.in_transaction(conn, services, item.owner_id)
            mode = (
                thread.conversation_mode
                if thread.conversation_mode in {"ask", "plan", "craft"}
                else "craft"
            )
            services.project_task_repo.insert(
                conn,
                ProjectTaskRow(
                    item.target_task_id,
                    item.owner_id,
                    body.project_id,
                    (thread.title or manifest["turns"][0]["prompt"] or thread.thread_id)[:128],
                    "migration_" + _hash([item.owner_id, thread.thread_id]),
                    None,
                    thread.created_at * 1000,
                    thread.last_active * 1000 or thread.created_at * 1000,
                    status="completed",
                    selected_expert_id=selected,
                    pinned=thread.pinned,
                    action_mode=mode,
                    approval_mode=policy.mode,
                    approval_revision=policy.revision,
                    approval_updated_at=int(policy.updated_at.timestamp() * 1000),
                ),
            )
            inserted_files: set[str] = set()
            for turn in manifest["turns"]:
                attached = []
                for file in turn["files"]:
                    if file["status"] != "copied":
                        continue
                    data = (stage / (file["attachment_id"] + ".bin")).read_bytes()
                    if (
                        len(data) != file["byte_count"]
                        or hashlib.sha256(data).hexdigest() != file["sha256"]
                    ):
                        raise SourceUnavailable("staged_attachment_corrupt")
                    row = ProjectTaskAttachmentRow(
                        file["attachment_id"],
                        item.owner_id,
                        item.target_task_id,
                        file["name"],
                        file["media_type"],
                        len(data),
                        file["sha256"],
                        turn["created_at"],
                    )
                    if row.attachment_id not in inserted_files:
                        services.project_task_attachment_repo.insert(conn, row, data)
                        inserted_files.add(row.attachment_id)
                    attached.append(input_attachments.view(row))
                configuration = {
                    "branch_origin": {
                        "source_thread_id": thread.thread_id,
                        "source_agent_id": thread.agent_id,
                        "source_message_ids": turn["message_ids"],
                        "operation": "legacy",
                        "history_only": True,
                        "source_status": turn["status"],
                    },
                    "expert_profile": {"expert_id": thread.agent_id, "name": agent.name},
                    "action_mode": mode,
                    "attachment_ids": [file.attachment_id for file in attached],
                    "attachments": [file.model_dump(mode="json") for file in attached],
                    "migrated_history": {
                        "source_model_ref": thread.model_ref,
                        "source_reasoning_mode": thread.reasoning_mode,
                        "source_reasoning_effort": thread.reasoning_effort,
                        "channel_type": thread.channel_type,
                        "files": turn["files"],
                        "trajectory": turn["trajectory"],
                        "source_digest": manifest["source_digest"],
                    },
                    "legacy_attachments": [
                        {
                            "name": file["name"],
                            "media_type": file.get("media_type", "application/octet-stream"),
                            "source_agent_id": file["source_agent_id"],
                            "workspace_path": file["source"].get("workspace_path")
                            or file["source"].get("path"),
                            "available": False,
                        }
                        for file in turn["files"]
                        if file["status"] != "copied"
                    ],
                }
                for index, block in enumerate(turn["blocks"]):
                    block["block_id"] = f"{turn['turn_id']}:{index + 1}"
                services.project_task_branch_repo.insert_history(
                    conn,
                    ProjectTaskTurnRow(
                        turn_id=turn["turn_id"],
                        task_id=item.target_task_id,
                        request_id="migration_history_" + _hash(turn["turn_id"]),
                        prompt=turn["prompt"],
                        reply=turn["reply"],
                        status=turn["status"],
                        error_code="",
                        activity="",
                        configuration_json=json.dumps(configuration),
                        model_ref="",
                        worker_id="",
                        cancel_requested=False,
                        created_at=turn["created_at"],
                        updated_at=turn["created_at"],
                        content_blocks_json=json.dumps(turn["blocks"]),
                        content_revision=1,
                    ),
                )
            services.project_task_legacy_source_repo.insert(
                conn,
                LegacyConversationSourceRow(
                    item.target_task_id,
                    item.owner_id,
                    "migration_" + item.item_id,
                    _hash([thread.thread_id, manifest["source_digest"]]),
                    thread.agent_id,
                    thread.thread_id,
                    manifest["source_digest"],
                    now,
                ),
            )
            if not UnifiedSourceRouteRepo.transition(
                conn,
                item.owner_id,
                route.route_id,
                route.revision,
                "draining",
                "task",
                task_id=item.target_task_id,
                source_digest=manifest["source_digest"],
                now=now,
            ):
                raise SourceUnavailable("source_changed")
            services.conversation_migration_repo.finish(
                conn,
                item,
                "completed",
                "",
                {
                    "source_digest": manifest["source_digest"],
                    "turn_count": len(manifest["turns"]),
                    "trajectory_event_count": manifest["trajectory_event_count"],
                    "attachment_count": len(inserted_files),
                    "missing_attachment_count": sum(
                        file["status"] != "copied"
                        for turn in manifest["turns"]
                        for file in turn["files"]
                    ),
                    "warnings": manifest["warnings"],
                    "model_calls": 0,
                },
                now,
            )
