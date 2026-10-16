"""Task-scoped approval choices, administrator limits and durable writes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.approval_models import (
    ApprovalCapabilities,
    ApprovalMode,
    ApprovalPolicyUpdate,
    TaskApprovalPolicy,
    TaskApprovalSettings,
)

if TYPE_CHECKING:
    from octop.infra.projects.tasks import TaskDetail

SETTINGS_KEY = "task_approval_policy"


def settings(services: SharedServices) -> TaskApprovalSettings:
    raw = services.settings_repo.get(SETTINGS_KEY)
    return TaskApprovalSettings.model_validate_json(raw) if raw else TaskApprovalSettings()


def save_settings(services: SharedServices, body: TaskApprovalSettings) -> TaskApprovalSettings:
    services.settings_repo.set(SETTINGS_KEY, body.model_dump_json())
    return body


def capabilities(services: SharedServices, model_ref: str | None = None) -> ApprovalCapabilities:
    if model_ref is None:
        from octop.infra.projects.tasks import resolve_model_ref

        model_ref = resolve_model_ref(services)
    return ApprovalCapabilities(
        allowed_modes=settings(services).allowed_modes,
        reviewer_ready=bool(
            model_ref and ProviderStore(services.provider_repo).is_model_ref_usable(model_ref)
        ),
    )


def assert_available(
    services: SharedServices, mode: ApprovalMode, model_ref: str | None = None
) -> None:
    available = capabilities(services, model_ref)
    if mode not in available.allowed_modes or (
        mode == "approve_for_me" and not available.reviewer_ready
    ):
        raise OctopError.localized(ErrorCode.TASK_APPROVAL_MODE_UNAVAILABLE, status=409)


def view(row: ProjectTaskRow) -> TaskApprovalPolicy:
    return TaskApprovalPolicy(
        mode=cast(ApprovalMode, row.approval_mode),
        revision=row.approval_revision,
        updated_at=datetime.fromtimestamp((row.approval_updated_at or row.created_at) / 1000, UTC),
    )


def snapshot(
    conn: Any, services: SharedServices, task_id: str, owner_id: int
) -> TaskApprovalPolicy:
    """Freeze the current Task policy inside the caller's owner-locked transaction."""
    row = services.project_task_repo.get_in_transaction(conn, task_id, owner_id)
    if row is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return view(row)


def set_policy(
    services: SharedServices, owner_id: int, task_id: str, body: ApprovalPolicyUpdate
) -> TaskDetail:
    from octop.infra.projects.tasks import detail, now_ms

    encoded = json.dumps(
        body.model_dump(exclude={"request_id"}), sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    with services.db.transaction() as conn:
        repo = services.project_task_repo
        if not repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        row = repo.get_in_transaction(conn, task_id, owner_id)
        if row is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        requests = json.loads(row.approval_requests_json)
        previous = next((item for item in requests if item["request_id"] == body.request_id), None)
        if previous is not None:
            if previous["digest"] != digest:
                raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT, status=409)
        else:
            if row.approval_revision != body.expected_revision:
                raise OctopError.localized(ErrorCode.TASK_APPROVAL_POLICY_CONFLICT, status=409)
            assert_available(services, body.mode)
            requests.append({"request_id": body.request_id, "digest": digest})
            if repo.update_approval(conn, row, body.mode, json.dumps(requests), now_ms()) is None:
                raise OctopError.localized(ErrorCode.TASK_APPROVAL_POLICY_CONFLICT, status=409)
    return detail(services, owner_id, task_id)
