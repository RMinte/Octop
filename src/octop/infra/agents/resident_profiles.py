"""Private professional instances with explicit, versioned expert-template adoption."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from pydantic import Field, StringConstraints

from octop.i18n import tr
from octop.infra.agents.experts.catalog import ExpertCatalog, discover_seed_paths
from octop.infra.agents.resident_conversation import RequestId, ResidentModel
from octop.infra.agents.resident_identity import (
    initialize_resident_binding,
    reserve_resident_in_transaction,
)
from octop.infra.db.repos.resident_profiles import ResidentProfileRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.ulid import new_ulid

TemplateKind = Literal["builtin", "published"]
_EXCLUDED = {"USER.md", "MEMORY.md", "TOOLS.md", "HEARTBEAT.md", "PROACTIVE.md"}


class ResidentProfileCreate(ResidentModel):
    request_id: RequestId
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    template_kind: TemplateKind
    template_id: str = Field(min_length=1, max_length=200)


class ResidentProfileAdopt(ResidentModel):
    expected_revision: int = Field(ge=1, description="Profile revision from the reviewed diff.")
    template_version: str = Field(
        pattern=r"^[a-f0-9]{64}$", description="Exact available version reviewed in template-diff."
    )


class ResidentProfileArchive(ResidentModel):
    expected_revision: int = Field(ge=1)
    archived: bool


class ResidentTemplateSnapshot(ResidentModel):
    kind: TemplateKind
    template_id: str
    label: str
    description: str
    files: dict[str, str]


class ResidentProfile(ResidentModel):
    profile_id: str
    agent_id: str
    thread_id: str
    name: str
    status: Literal["initializing", "ready", "failed"]
    error_code: str | None
    template_kind: TemplateKind
    template_id: str
    template_version: str
    revision: int
    archived: bool
    tools_enabled: Literal[False] = False
    created_at: datetime
    updated_at: datetime


class ResidentProfileList(ResidentModel):
    profiles: list[ResidentProfile]


class ResidentTemplateChange(ResidentModel):
    path: str
    before: str | None
    after: str | None


class ResidentTemplateDiff(ResidentModel):
    profile_id: str
    revision: int
    current_version: str
    available_version: str
    has_changes: bool
    changes: list[ResidentTemplateChange]


def _canonical(snapshot: ResidentTemplateSnapshot) -> str:
    return json.dumps(
        snapshot.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fingerprint(body: ResidentProfileCreate) -> str:
    return _hash(body.model_dump_json(exclude={"request_id"}))


def _template(
    services: SharedServices,
    catalog: ExpertCatalog | None,
    kind: TemplateKind,
    template_id: str,
    locale: str,
) -> ResidentTemplateSnapshot:
    if kind == "builtin":
        expert = catalog.get(template_id) if catalog is not None else None
        if expert is None or catalog is None or not catalog.is_bundled(template_id):
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        summary = expert.summary
        label = summary.label_zh if locale == "zh" else summary.label_en
        description = summary.description_zh if locale == "zh" else summary.description_en
        root = catalog.expert_dir(template_id)
        paths = expert.files
    else:
        # Published templates are readable by every authenticated user in the existing catalog.
        # Their source Agent, permissions and runtime state are never reused.
        published = services.published_expert_repo.get(template_id)
        if published is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        label, description = published.name, published.description
        root = services.paths.published_experts_dir / published.id
        if not root.is_dir():
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        paths = discover_seed_paths(root)
    files: dict[str, str] = {}
    total = 0
    for relative in sorted(paths):
        # Only persona/method documents; never seed tools, subagents, user memory or schedules.
        if (
            "/" in relative
            or "\\" in relative
            or not relative.endswith(".md")
            or relative in _EXCLUDED
        ):
            continue
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        total += len(text)
        if total > 262144:
            raise OctopError.localized(ErrorCode.RESIDENT_TEMPLATE_INVALID)
        files[relative] = text
    return ResidentTemplateSnapshot(
        kind=kind,
        template_id=template_id,
        label=label,
        description=description,
        files=files,
    )


def _prompt(snapshot: ResidentTemplateSnapshot, locale: str) -> str:
    template = "\n\n".join(
        [snapshot.label, snapshot.description]
        + [f"{path}\n{text}" for path, text in snapshot.files.items()]
    )
    return "\n\n".join(
        [
            tr("agents.resident.profile_system_prompt", locale, template=template),
            tr("agents.resident.system_prompt", locale),
        ]
    )


def _require(services: SharedServices, owner_id: int, profile_id: str) -> ResidentProfileRow:
    row = services.resident_profile_repo.get(owner_id, profile_id)
    if row is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    binding = services.resident_conversation_repo.get_by_thread(row.thread_id)
    agent = services.agent_repo.get(row.agent_id)
    if (
        binding is None
        or binding.owner_id != owner_id
        or binding.agent_id != row.agent_id
        or binding.role != f"profile:{profile_id}"
        or agent is None
        or agent.user_id != owner_id
        or agent.is_shared
        or agent.kind != "expert"
    ):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return row


def profile_view(services: SharedServices, row: ResidentProfileRow) -> ResidentProfile:
    binding = services.resident_conversation_repo.get_by_thread(row.thread_id)
    if binding is None or binding.owner_id != row.owner_id:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return ResidentProfile(
        profile_id=row.profile_id,
        agent_id=row.agent_id,
        thread_id=row.thread_id,
        name=row.name,
        status=cast(Literal["initializing", "ready", "failed"], binding.status),
        error_code=binding.error_code or None,
        template_kind=cast(TemplateKind, row.template_kind),
        template_id=row.template_id,
        template_version=row.template_version,
        revision=row.revision,
        archived=row.archived,
        created_at=datetime.fromtimestamp(row.created_at / 1000, tz=UTC),
        updated_at=datetime.fromtimestamp(row.updated_at / 1000, tz=UTC),
    )


def list_profiles(services: SharedServices, owner_id: int) -> ResidentProfileList:
    return ResidentProfileList(
        profiles=[
            profile_view(services, row)
            for row in services.resident_profile_repo.list_by_owner(owner_id)
        ]
    )


def get_profile(services: SharedServices, owner_id: int, profile_id: str) -> ResidentProfile:
    return profile_view(services, _require(services, owner_id, profile_id))


def _reserve_profile(
    services: SharedServices,
    catalog: ExpertCatalog | None,
    owner_id: int,
    body: ResidentProfileCreate,
) -> ResidentProfileRow:
    fingerprint = _fingerprint(body)
    existing = services.resident_profile_repo.get_request(owner_id, body.request_id)
    if existing is not None:
        if existing.fingerprint != fingerprint:
            raise OctopError.localized(ErrorCode.RESIDENT_REQUEST_CONFLICT)
        return existing
    user = services.user_repo.get(owner_id)
    if user is None or user.disabled:
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    snapshot = _template(services, catalog, body.template_kind, body.template_id, user.locale)
    snapshot_json = _canonical(snapshot)
    now = time.time_ns() // 1_000_000
    profile_id, agent_id, thread_id = new_ulid(), new_ulid(), f"thr_{new_ulid()}"
    with services.db.transaction() as conn:
        if not services.user_repo.lock_identity_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        existing = services.resident_profile_repo.request_in_transaction(
            conn, owner_id, body.request_id
        )
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise OctopError.localized(ErrorCode.RESIDENT_REQUEST_CONFLICT)
            return existing
        name = reserve_resident_in_transaction(
            services,
            conn,
            owner_id=owner_id,
            agent_id=agent_id,
            thread_id=thread_id,
            name=body.name,
            system_prompt=_prompt(snapshot, user.locale),
            now=now,
            role=f"profile:{profile_id}",
        )
        row = ResidentProfileRow(
            profile_id=profile_id,
            owner_id=owner_id,
            request_id=body.request_id,
            fingerprint=fingerprint,
            agent_id=agent_id,
            thread_id=thread_id,
            name=name,
            template_kind=body.template_kind,
            template_id=body.template_id,
            template_version=_hash(snapshot_json),
            snapshot_json=snapshot_json,
            revision=1,
            archived=False,
            created_at=now,
            updated_at=now,
        )
        services.resident_profile_repo.insert_in_transaction(conn, row)
    return row


async def create_profile(
    services: SharedServices,
    catalog: ExpertCatalog | None,
    owner_id: int,
    body: ResidentProfileCreate,
) -> ResidentProfile:
    row = await asyncio.to_thread(_reserve_profile, services, catalog, owner_id, body)
    binding = await asyncio.to_thread(
        services.resident_conversation_repo.get_by_thread, row.thread_id
    )
    assert binding is not None
    await initialize_resident_binding(services, binding)
    return await asyncio.to_thread(profile_view, services, row)


def template_diff(
    services: SharedServices,
    catalog: ExpertCatalog | None,
    owner_id: int,
    profile_id: str,
) -> ResidentTemplateDiff:
    row = _require(services, owner_id, profile_id)
    user = services.user_repo.get(owner_id)
    assert user is not None
    current = ResidentTemplateSnapshot.model_validate_json(row.snapshot_json)
    available = _template(
        services, catalog, cast(TemplateKind, row.template_kind), row.template_id, user.locale
    )
    available_version = _hash(_canonical(available))
    before = {"label": current.label, "description": current.description, **current.files}
    after = {"label": available.label, "description": available.description, **available.files}
    changes = [
        ResidentTemplateChange(path=key, before=before.get(key), after=after.get(key))
        for key in sorted(before.keys() | after.keys())
        if before.get(key) != after.get(key)
    ]
    return ResidentTemplateDiff(
        profile_id=profile_id,
        revision=row.revision,
        current_version=row.template_version,
        available_version=available_version,
        has_changes=row.template_version != available_version,
        changes=changes,
    )


def adopt_template(
    services: SharedServices,
    catalog: ExpertCatalog | None,
    owner_id: int,
    profile_id: str,
    body: ResidentProfileAdopt,
) -> ResidentProfile:
    row = _require(services, owner_id, profile_id)
    user = services.user_repo.get(owner_id)
    assert user is not None
    snapshot = _template(
        services, catalog, cast(TemplateKind, row.template_kind), row.template_id, user.locale
    )
    snapshot_json = _canonical(snapshot)
    if _hash(snapshot_json) != body.template_version:
        raise OctopError.localized(ErrorCode.RESIDENT_TEMPLATE_CHANGED)
    with services.db.transaction() as conn:
        if not services.user_repo.lock_identity_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        locked_row = services.resident_profile_repo.get_in_transaction(conn, owner_id, profile_id)
        if locked_row is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        row = locked_row
        if row.revision != body.expected_revision:
            raise OctopError.localized(ErrorCode.RESIDENT_PROFILE_CONFLICT)
        if row.template_version != body.template_version:
            now = time.time_ns() // 1_000_000
            if not services.resident_profile_repo.adopt_in_transaction(
                conn,
                owner_id=owner_id,
                profile_id=profile_id,
                expected_revision=body.expected_revision,
                template_version=body.template_version,
                snapshot_json=snapshot_json,
                now=now,
            ):
                raise OctopError.localized(ErrorCode.RESIDENT_PROFILE_CONFLICT)
            if not services.agent_repo.update_identity_prompt_in_transaction(
                conn,
                agent_id=row.agent_id,
                user_id=owner_id,
                system_prompt=_prompt(snapshot, user.locale),
                now=now // 1000,
            ):
                raise OctopError.localized(ErrorCode.NOT_FOUND)
    return get_profile(services, owner_id, profile_id)


def archive_profile(
    services: SharedServices,
    owner_id: int,
    profile_id: str,
    body: ResidentProfileArchive,
) -> ResidentProfile:
    _require(services, owner_id, profile_id)
    with services.db.transaction() as conn:
        if not services.user_repo.lock_identity_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        row = services.resident_profile_repo.get_in_transaction(conn, owner_id, profile_id)
        if row is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if row.revision != body.expected_revision:
            raise OctopError.localized(ErrorCode.RESIDENT_PROFILE_CONFLICT)
        if row.archived != body.archived:
            if body.archived and services.resident_request_repo.active_in_transaction(
                conn, owner_id, row.thread_id
            ):
                raise OctopError.localized(ErrorCode.AGENT_BUSY)
            now = time.time_ns() // 1_000_000
            if not services.resident_profile_repo.archive_in_transaction(
                conn,
                owner_id=owner_id,
                profile_id=profile_id,
                expected_revision=body.expected_revision,
                archived=body.archived,
                now=now,
            ):
                raise OctopError.localized(ErrorCode.RESIDENT_PROFILE_CONFLICT)
            if body.archived:
                services.resident_followup_repo.cancel_for_agent_in_transaction(
                    conn, owner_id, row.agent_id, now
                )
    return get_profile(services, owner_id, profile_id)
