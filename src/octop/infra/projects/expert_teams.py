"""Freeze persisted team rosters and derive each member's task-scoped abilities."""

from __future__ import annotations

import json
from typing import Any

from octop_harness.backends.workspace import BackendWorkspace

from octop.infra.agents.teams.service import TEAM_MANIFEST_WORKSPACE, TEAM_MIN_MEMBERS
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.knowledge import selection_snapshot
from octop.infra.projects.models import ProjectFields
from octop.infra.projects.service import _validate


def member_ids(services: SharedServices, owner_id: int, workspace: BackendWorkspace) -> list[str]:
    from octop.infra.projects.expert_profiles import expert_configuration

    text = workspace.read_text(TEAM_MANIFEST_WORKSPACE, limit=65537)
    if text is None or len(text) > 65536:
        raise OctopError.localized(ErrorCode.TEAM_MEMBER_INVALID)
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OctopError.localized(ErrorCode.TEAM_MEMBER_INVALID) from exc
    raw = (
        (manifest.get("members") or manifest.get("member")) if isinstance(manifest, dict) else None
    )
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise OctopError.localized(ErrorCode.TEAM_MEMBER_INVALID)
    result = list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
    if len(result) < TEAM_MIN_MEMBERS:
        raise OctopError.localized(ErrorCode.TEAM_MEMBERS_TOO_FEW)
    for expert_id in result:
        row, config = expert_configuration(services, owner_id, expert_id)
        if row.kind != "expert":
            raise OctopError.localized(ErrorCode.TEAM_MEMBER_INVALID)
        from octop.infra.projects.expert_profiles import _workspace

        _workspace(services, row, config)
    return result


def snapshot(
    services: SharedServices, owner_id: int, host_id: str, workspace: BackendWorkspace
) -> dict[str, Any]:
    from octop.infra.projects.expert_profiles import resolve_expert_profile

    roster = member_ids(services, owner_id, workspace)
    return {
        "host_id": host_id,
        "member_ids": roster,
        "members": [resolve_expert_profile(services, owner_id, expert_id) for expert_id in roster],
    }


def member_configuration(
    services: SharedServices,
    owner_id: int,
    root: dict[str, Any],
    profile: dict[str, Any],
    *,
    cached_skills: bool = False,
) -> dict[str, Any]:
    """Use project defaults plus this member, never the host's capabilities or persona."""
    from octop.infra.projects.expert_profiles import (
        expert_configuration,
        validate_expert_workspace_skills,
    )

    expert_configuration(services, owner_id, str(profile["expert_id"]))
    defaults = dict(root["project_defaults"])
    capabilities = profile.get("capabilities") or {}
    for key in ("skills", "mcp_servers", "plugins", "subagents"):
        defaults[key] = list(
            {
                json.dumps(item, sort_keys=True): item
                for item in [*capabilities.get(key, []), *defaults.get(key, [])]
            }.values()
        )
    overrides = root.get("execution_options") or {}
    defaults.update(
        {key: value for key, value in overrides.items() if key in ProjectFields.model_fields}
    )
    # This roster is fixed by the selected team; members cannot create another team.
    defaults["team_enabled"] = False
    fields = ProjectFields.model_validate(defaults)
    if len({skill.slug for skill in fields.skills}) != len(fields.skills):
        raise OctopError.localized(ErrorCode.PROJECT_RESOURCE_INVALID)
    validate_expert_workspace_skills(
        profile, fields.skills, inherit_workspace=overrides.get("skills") is None
    )
    _validate(services, owner_id, fields, cached_skills=cached_skills)
    return {
        **root,
        **fields.model_dump(mode="json"),
        **selection_snapshot(
            services,
            owner_id,
            overrides.get("knowledge_base_ids")
            if overrides.get("knowledge_base_ids") is not None
            else capabilities.get("knowledge_base_ids") or None,
        ),
        "expert_profile": profile,
    }
