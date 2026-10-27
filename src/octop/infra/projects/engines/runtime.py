"""Prepare task-owned context and bind ACP sessions to their actual execution host."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from octop_harness.backends import BackendWorkspace, resolve_backend

from octop.i18n import tr
from octop.infra.agents.acp_setup import AUTH_SCOPE_VERSION, ACPSetupService
from octop.infra.agents.settings.acp import ACPSettingsStore
from octop.infra.connectors.service import ConnectorService
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import input_attachments
from octop.infra.projects.engine_catalog import (
    EngineCatalogEntry,
    EngineSelection,
    catalog,
    validate_configuration,
    validate_engine,
)
from octop.infra.projects.engines.acp import ACPFeatureError, ACPTaskSession, SessionInfo
from octop.infra.projects.engines.base import EngineRequest, TaskEvents
from octop.infra.projects.engines.handoff import build_handoff, history_rows
from octop.infra.projects.engines.permissions import EnginePermissions
from octop.infra.projects.expert_memory import ExpertMemory
from octop.infra.projects.expert_profiles import assert_profile_access, profile_prompt
from octop.infra.projects.models import ProjectFields
from octop.infra.projects.recovery import TaskInterruption, diagnostic
from octop.infra.projects.resource_snapshot import expert_skill_files, load_snapshot, save_snapshot
from octop.infra.projects.service import _validate
from octop.infra.projects.source_access import (
    assert_source_access,
    external_unverified,
    restrict_external_configuration,
)
from octop.infra.projects.tasks import now_ms
from octop.infra.utils.locale import normalize_locale


def _invalid(feature: str) -> OctopError:
    return OctopError.localized(
        ErrorCode.TASK_SETTINGS_INVALID,
        status=409,
        setting="engine",
        details={"setting": "engine", "reason": "unsupported_capability", "capability": feature},
    )


def _launch(
    services: SharedServices, owner: int, selection: EngineSelection
) -> tuple[list[str], dict[str, str], ACPSettingsStore]:
    validate_engine(services, owner, selection)
    store = ACPSettingsStore(settings_repo=services.settings_repo, agents_repo=services.agent_repo)
    setup = ACPSetupService(store, services.paths.root)
    config = store.load_runners(owner, migrate_legacy=False).get(selection.connection_id or "")
    if config is None:
        raise _invalid("connection")
    launch = setup._launch(selection.connection_id or "", config)
    if not launch:
        raise _invalid("installation")
    return (
        launch,
        setup.runner_environment(owner, selection.connection_id or "", config, ensure=True),
        store,
    )


def _model_options(info: SessionInfo) -> list[dict[str, str]]:
    result = []

    def collect(options: list[dict[str, Any]]) -> None:
        for option in options:
            if isinstance(option.get("options"), list):
                collect(option["options"])
            elif isinstance(option.get("value"), str):
                result.append(
                    {"model_id": option["value"], "label": option.get("name") or option["value"]}
                )

    for item in info.config_options:
        if item.get("category") == "model":
            collect(item.get("options") or [])
    return result


async def inspect_engine(
    services: SharedServices, owner_id: int, selection: EngineSelection
) -> EngineCatalogEntry:
    entry = await asyncio.to_thread(validate_engine, services, owner_id, selection)
    if selection.engine_id == "octop":
        return entry
    launch, env, store = await asyncio.to_thread(_launch, services, owner_id, selection)
    # No model prompt, tools, project, or user directory is sent during discovery.
    import tempfile

    directory = await asyncio.to_thread(tempfile.TemporaryDirectory, prefix="octop-engine-")
    session = ACPTaskSession(launch, env, directory.name)
    try:
        info = await session.open()

        def save() -> None:
            current = validate_engine(services, owner_id, selection)
            if current.config_digest != entry.config_digest:
                raise _invalid("connection_changed")
            observation = store.load_setup_status(owner_id, selection.connection_id or "")
            observation.update(
                engine_models=_model_options(info),
                engine_capabilities={
                    "images": info.prompt_images,
                    "native_resume": info.supports_load,
                    "model_selection": bool(info.model_ids),
                },
            )
            store.save_setup_status(owner_id, selection.connection_id or "", observation)

        await asyncio.to_thread(save)
    finally:
        await session.close()
        await asyncio.to_thread(directory.cleanup)
    return next(
        item
        for item in await asyncio.to_thread(catalog, services, owner_id)
        if item.engine.engine_id == selection.engine_id
        and item.engine.connection_id == selection.connection_id
    )


def _mcp_servers(configs: dict[str, Any]) -> list[Any]:
    from acp.schema import EnvVariable, HttpHeader, HttpMcpServer, McpServerStdio, SseMcpServer

    result: list[Any] = []
    for name, spec in configs.items():
        if spec.get("command"):
            result.append(
                McpServerStdio(
                    name=name,
                    command=spec["command"],
                    args=spec.get("args") or [],
                    env=[
                        EnvVariable(name=key, value=str(value))
                        for key, value in (spec.get("env") or {}).items()
                    ],
                )
            )
        elif spec.get("url"):
            transport = "sse" if spec.get("transport") == "sse" else "http"
            cls = SseMcpServer if transport == "sse" else HttpMcpServer
            result.append(
                cls.model_validate(
                    {
                        "type": transport,
                        "name": name,
                        "url": spec["url"],
                        "headers": [
                            HttpHeader(name=key, value=str(value))
                            for key, value in (spec.get("headers") or {}).items()
                        ],
                    }
                )
            )
        else:
            raise _invalid("mcp_transport")
    return result


async def run_acp_turn(request: EngineRequest) -> TaskEvents:
    services, owner, turn = request.services, request.owner_id, request.turn
    raw = restrict_external_configuration(json.loads(turn.configuration_json))
    frozen = raw.get("execution_engine") or {}
    selection = EngineSelection.model_validate(
        {key: frozen[key] for key in EngineSelection.model_fields if key in frozen}
    )
    if external_unverified(raw):
        raise _invalid("source_authority")
    if raw.get("source_origin"):
        await asyncio.to_thread(
            assert_source_access, services, owner, turn.task_id, raw["source_origin"]
        )
    entry = await asyncio.to_thread(
        validate_engine,
        services,
        owner,
        selection,
        action_mode=str(raw.get("action_mode") or "craft"),
    )
    if entry.config_digest != (raw.get("execution_engine") or {}).get("config_digest"):
        raise _invalid("connection_changed")
    validate_configuration(raw)
    if turn.prompt.strip() == "/compact":
        raise _invalid("compact")
    if turn.checkpoint_id or turn.recovery_json:
        raise TaskInterruption(
            diagnostic("engine", RuntimeError(), can_resume=False, needs_reconciliation=True),
            "prepare",
        )

    fields = ProjectFields.model_validate(
        {key: raw[key] for key in ProjectFields.model_fields if key in raw}
    )
    await asyncio.to_thread(_validate, services, owner, fields)
    runtime_dir = services.paths.root / "project-tasks" / turn.task_id
    workspace = Path(fields.directory) if fields.directory else runtime_dir / "workspace"
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    user = await asyncio.to_thread(services.user_repo.get, owner)
    locale = normalize_locale(user.locale if user else "zh")
    system_relative = f".octop/tasks/{turn.task_id}/{turn.turn_id}"
    backend = await asyncio.to_thread(
        resolve_backend,
        {"type": "local_shell", "root_dir": str(workspace), "virtual_mode": True},
        workspace_dir=workspace,
    )
    filespace = BackendWorkspace(backend, workspace, system_files_path=system_relative)
    await input_attachments.materialize_files(services, owner, turn.task_id, filespace)

    from octop.infra.projects.runtime import _skill_files

    snapshot_dir = runtime_dir / turn.turn_id / "resources"
    files = await asyncio.to_thread(load_snapshot, snapshot_dir, turn.configuration_json)
    if files is None:
        files = await asyncio.to_thread(_skill_files, services, fields)
        if raw.get("execution_options", {}).get("skills") is None:
            files.extend(expert_skill_files(raw.get("expert_profile") or {}))
        await asyncio.to_thread(save_snapshot, snapshot_dir, turn.configuration_json, files)
    skill_paths = []
    for slug, entries in files:
        for name, data in entries:
            await filespace.aupload_bytes(filespace.system_rel(f"skills/{slug}/{name}"), data)
        skill_paths.append(filespace.system_rel(f"skills/{slug}/SKILL.md"))

    profile = raw.get("expert_profile") or {}
    if profile:
        await asyncio.to_thread(assert_profile_access, services, owner, profile)
    task = await asyncio.to_thread(services.project_task_repo.get, turn.task_id, owner)
    if task is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    memory = (
        ExpertMemory(
            services,
            owner,
            turn.task_id,
            turn.turn_id,
            task.project_id,
            profile,
            enabled=raw.get("expert_memory_enabled"),
            worker_id=turn.worker_id,
        )
        if profile
        else None
    )
    recalled = await memory.prompt(turn.prompt) if memory else ""
    context = "\n\n".join(
        part
        for part in [
            profile_prompt(profile, locale) if profile else "",
            str(fields.instructions or ""),
            recalled,
            "Selected Skill instructions (read the relevant SKILL.md before using it):\n"
            + "\n".join(skill_paths)
            if skill_paths
            else "",
        ]
        if part
    )
    rows = await asyncio.to_thread(services.project_task_turn_repo.list, turn.task_id)
    journal = history_rows(rows)
    history_file = runtime_dir / turn.turn_id / "engine-history.json"

    def export() -> None:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history_file.write_text(json.dumps(journal, ensure_ascii=False), encoding="utf-8")

    await asyncio.to_thread(export)
    connectors = ConnectorService(
        repo=services.connector_repo,
        secret_repo=services.secret_repo,
        settings_repo=services.settings_repo,
        config=services.config,
    )
    configs = await connectors.mcp_configs_for_user(owner) if fields.mcp_servers else {}
    if not set(fields.mcp_servers) <= configs.keys():
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    mcp = _mcp_servers({name: configs[name] for name in fields.mcp_servers})
    from acp.schema import EnvVariable, McpServerStdio

    mcp.append(
        McpServerStdio(
            name="octop_task_history",
            command=sys.executable,
            args=["-m", "octop.infra.projects.engines.history_mcp", str(history_file)],
            env=[EnvVariable(name="PYTHONPATH", value=str(Path(__file__).resolve().parents[4]))],
        )
    )
    launch, env, _ = await asyncio.to_thread(_launch, services, owner, selection)
    key = hashlib.sha256(
        json.dumps(
            {
                "engine": selection.model_dump(),
                "auth_scope_version": AUTH_SCOPE_VERSION,
                "config": entry.config_digest,
                "cwd": str(workspace.resolve()),
                "profile": profile,
                "instructions": fields.instructions,
                "skills": [
                    (slug, [(name, hashlib.sha256(data).hexdigest()) for name, data in entries])
                    for slug, entries in files
                ],
                "mcp": fields.mcp_servers,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    binding = await asyncio.to_thread(
        services.project_task_repo.load_engine_session,
        owner,
        turn.task_id,
        turn.turn_id,
        turn.worker_id,
        key,
    )
    previous = next(
        (row for row in rows if binding and row.turn_id == binding.get("turn_id")), None
    )
    parked = (
        binding
        if binding
        and binding.get("status") == "completed"
        and previous
        and previous.status == "completed"
        else None
    )
    session = ACPTaskSession(launch, env, str(workspace), mcp, EnginePermissions(request, locale))
    dispatched = False
    reply = ""
    execution_task = asyncio.current_task()
    authority_failure: Exception | None = None

    def check_authority() -> None:
        current_user = services.user_repo.get(owner)
        if current_user is None or current_user.disabled:
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        current = validate_engine(services, owner, selection)
        if current.config_digest != entry.config_digest:
            raise _invalid("connection_changed")
        if profile:
            assert_profile_access(services, owner, profile)
        if raw.get("source_origin"):
            assert_source_access(services, owner, turn.task_id, raw["source_origin"])

    async def guard_authority() -> None:
        nonlocal authority_failure
        while True:
            await asyncio.sleep(1)
            try:
                await asyncio.to_thread(check_authority)
            except Exception as exc:
                authority_failure = exc
                if execution_task is not None:
                    execution_task.cancel()
                return

    await asyncio.to_thread(check_authority)
    guard = asyncio.create_task(guard_authority())
    try:
        info = await session.open(str(parked["session_id"]) if parked else None)
        if mcp and any(
            getattr(server, "type", "stdio") in {"http", "sse"}
            and not (info.capabilities.get("mcpCapabilities") or {}).get(server.type)
            for server in mcp
        ):
            raise _invalid("mcp_transport")
        after = int(parked.get("watermark", 0)) if parked and info.resumed else 0
        actual_model_id = next(
            (
                item.get("currentValue")
                for item in info.config_options
                if item.get("category") == "model"
            ),
            None,
        )
        workspace_scope = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
        if not await asyncio.to_thread(
            services.project_task_turn_repo.save_engine_facts,
            turn,
            {
                "actual_model_id": actual_model_id,
                "resumed": info.resumed,
                "workspace_scope": workspace_scope,
            },
            now_ms(),
        ):
            raise asyncio.CancelledError
        handoff = build_handoff(journal, after=after)
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": f"<octop_task_context>\nWorking directory: {workspace}\n{context}\n</octop_task_context>\n\n{handoff}\n\nCurrent user request:\n{turn.prompt}",
            }
        ]
        attachment_map = {
            item.attachment_id: item
            for item in await asyncio.to_thread(
                input_attachments.for_task, services, owner, turn.task_id
            )
        }
        for identifier in raw.get("attachment_ids", []):
            attachment = attachment_map[identifier]
            if input_attachments.is_image(attachment.media_type):
                if not info.prompt_images:
                    raise _invalid("images")
                _, data = await asyncio.to_thread(
                    input_attachments.download, services, owner, identifier, task_id=turn.task_id
                )
                data, media_type = await asyncio.to_thread(
                    input_attachments.model_image_bytes, data, attachment.media_type
                )
                blocks.append(
                    {
                        "type": "image",
                        "data": base64.b64encode(data).decode("ascii"),
                        "mimeType": media_type,
                    }
                )
            elif attachment.workspace_path:
                blocks.append(
                    {
                        "type": "text",
                        "text": f"Attached file: {attachment.name}\nWorkspace path: {attachment.workspace_path}",
                    }
                )

        async def save(status: str) -> None:
            if not await asyncio.to_thread(
                services.project_task_repo.save_engine_session,
                owner,
                turn.task_id,
                turn.turn_id,
                turn.worker_id,
                key,
                {
                    "session_id": info.session_id,
                    "status": status,
                    "turn_id": turn.turn_id,
                    "watermark": turn.pk if status == "completed" else after,
                    "workspace_scope": hashlib.sha256(
                        str(workspace.resolve()).encode()
                    ).hexdigest(),
                },
            ):
                raise asyncio.CancelledError

        await save("inflight")
        yield {
            "type": "progress",
            "call_id": f"engine:{turn.turn_id}",
            "content": tr(
                "projects.engine_resumed" if info.resumed else "projects.engine_started",
                locale,
                engine=entry.label,
            ),
        }
        dispatched = True
        async with contextlib.aclosing(
            session.stream(blocks, model_id=selection.model_id)
        ) as events:
            async for event in events:
                if event.get("type") == "token":
                    reply += event["content"]
                # Native session usage is cumulative and must not be added to
                # Octop's per-provider billing ledger as if it were turn usage.
                if event.get("type") not in {"usage", "usage_context"}:
                    yield event
        await save("completed")
        actual_model_id = next(
            (
                item.get("currentValue")
                for item in info.config_options
                if item.get("category") == "model"
            ),
            None,
        )
        await asyncio.to_thread(
            services.project_task_turn_repo.save_engine_facts,
            turn,
            {"actual_model_id": actual_model_id if isinstance(actual_model_id, str) else None},
            now_ms(),
        )
        if memory is not None:
            await memory.capture(turn.prompt, reply)
    except ACPFeatureError as exc:
        raise _invalid(exc.feature) from None
    except asyncio.CancelledError:
        if authority_failure is not None:
            raise authority_failure from None
        raise
    except OctopError:
        raise
    except Exception as exc:
        raise TaskInterruption(
            diagnostic(
                "engine_prompt" if dispatched else "engine_prepare",
                exc,
                can_resume=False,
                needs_reconciliation=dispatched,
                resource_type="engine",
                resource_id=selection.connection_id,
            ),
            "prepare",
        ) from None
    finally:
        guard.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await guard
        await session.close()
