"""Owner-scoped channel bindings to projects and optional expert profiles."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, get_args

from psycopg.errors import UniqueViolation

from octop.infra.db.repos.channels import ChannelRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.expert_profiles import expert_configuration
from octop.infra.utils.ulid import new_ulid


@dataclass(frozen=True)
class ChannelBindingSpec:
    kind: str
    name: str
    config: dict[str, Any]
    selected_expert_id: str | None = None
    project_id: str | None = None
    action_mode: str = "craft"
    enabled: bool = True


def get(services: SharedServices, owner: int, channel_id: str) -> ChannelRow:
    row = services.channel_repo.get(channel_id)
    if (
        row is None
        or row.user_id != owner
        or (row.agent_id and services.resident_conversation_repo.get_by_agent(row.agent_id))
    ):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return row


def list_owned(services: SharedServices, owner: int) -> list[ChannelRow]:
    return [
        row
        for row in services.channel_repo.list_owned(owner)
        if not row.agent_id
        or services.resident_conversation_repo.get_by_agent(row.agent_id) is None
    ]


def _validate(services: SharedServices, owner: int, spec: ChannelBindingSpec) -> None:
    from octop.infra.gateway.gateway import ChannelKind

    user = services.user_repo.get(owner)
    if user is None or user.disabled:
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    kinds = get_args(ChannelKind) or tuple(getattr(ChannelKind, "__members__", {}).values())
    if (
        spec.kind not in {str(getattr(kind, "value", kind)) for kind in kinds}
        or not spec.name.strip()
    ):
        raise OctopError.localized(ErrorCode.PROJECT_RESOURCE_INVALID, status=422)
    if spec.action_mode not in {"ask", "plan", "craft"}:
        raise OctopError.localized(ErrorCode.PROJECT_RESOURCE_INVALID, status=422)
    if spec.selected_expert_id:
        expert_configuration(services, owner, spec.selected_expert_id)
    if spec.project_id:
        project = services.project_repo.get(spec.project_id, owner)
        if project is None or project.archived:
            raise OctopError.localized(ErrorCode.NOT_FOUND)


def _config(spec: ChannelBindingSpec) -> str:
    return json.dumps(
        {
            **spec.config,
            "_octop_task": {"project_id": spec.project_id, "action_mode": spec.action_mode},
        },
        ensure_ascii=False,
    )


def create(services: SharedServices, owner: int, spec: ChannelBindingSpec) -> ChannelRow:
    _validate(services, owner, spec)
    if services.channel_repo.get_by_owner_agent_name(
        owner, spec.selected_expert_id, spec.name.strip()
    ):
        raise OctopError.localized(ErrorCode.CHANNEL_NAME_TAKEN, status=409)
    identifier = new_ulid()
    try:
        services.channel_repo.create(
            channel_id=identifier,
            agent_id=spec.selected_expert_id,
            user_id=owner,
            kind=spec.kind,
            name=spec.name.strip(),
            config_json=_config(spec),
        )
    except (sqlite3.IntegrityError, UniqueViolation) as exc:
        if services.channel_repo.get_by_owner_agent_name(
            owner, spec.selected_expert_id, spec.name.strip()
        ):
            raise OctopError.localized(ErrorCode.CHANNEL_NAME_TAKEN, status=409) from exc
        raise
    if not spec.enabled:
        services.channel_repo.update(identifier, enabled=False)
    return get(services, owner, identifier)


def update(
    services: SharedServices, owner: int, channel_id: str, patch: dict[str, Any]
) -> ChannelRow:
    old = get(services, owner, channel_id)
    config = json.loads(old.config_json or "{}")
    binding = config.pop("_octop_task", {})
    spec = ChannelBindingSpec(
        kind=patch.get("kind", old.kind),
        name=patch.get("name", old.name),
        config=patch.get("config", config),
        selected_expert_id=patch.get("selected_expert_id", old.agent_id),
        project_id=patch.get("project_id", binding.get("project_id")),
        action_mode=patch.get("action_mode", binding.get("action_mode", "craft")),
        enabled=patch.get("enabled", bool(old.enabled)),
    )
    _validate(services, owner, spec)
    duplicate = services.channel_repo.get_by_owner_agent_name(
        owner, spec.selected_expert_id, spec.name.strip()
    )
    if duplicate is not None and duplicate.channel_id != channel_id:
        raise OctopError.localized(ErrorCode.CHANNEL_NAME_TAKEN, status=409)
    try:
        services.channel_repo.update(
            channel_id,
            agent_id=spec.selected_expert_id,
            kind=spec.kind,
            name=spec.name.strip(),
            config_json=_config(spec),
            enabled=spec.enabled,
        )
    except (sqlite3.IntegrityError, UniqueViolation) as exc:
        duplicate = services.channel_repo.get_by_owner_agent_name(
            owner, spec.selected_expert_id, spec.name.strip()
        )
        if duplicate is not None and duplicate.channel_id != channel_id:
            raise OctopError.localized(ErrorCode.CHANNEL_NAME_TAKEN, status=409) from exc
        raise
    return get(services, owner, channel_id)


def delete(services: SharedServices, owner: int, channel_id: str) -> None:
    get(services, owner, channel_id)
    services.channel_repo.delete(channel_id)
