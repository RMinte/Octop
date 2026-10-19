"""Account-wide task approval choices, mandatory human review and durable writes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.repos.user_task_approval_policies import UserTaskApprovalPolicyRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.approval_models import (
    APPROVAL_MODES,
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
    values = json.loads(raw) if raw else {}
    return TaskApprovalSettings(require_human_tools=values.get("require_human_tools", []))


def save_settings(services: SharedServices, body: TaskApprovalSettings) -> TaskApprovalSettings:
    services.settings_repo.set(SETTINGS_KEY, body.model_dump_json())
    return body


def capabilities(services: SharedServices, model_ref: str | None = None) -> ApprovalCapabilities:
    if model_ref is None:
        from octop.infra.projects.tasks import resolve_model_ref

        model_ref = resolve_model_ref(services)
    return ApprovalCapabilities(
        allowed_modes=list(APPROVAL_MODES),
        reviewer_ready=bool(
            model_ref and ProviderStore(services.provider_repo).is_model_ref_usable(model_ref)
        ),
    )


def view(row: UserTaskApprovalPolicyRow) -> TaskApprovalPolicy:
    return TaskApprovalPolicy(
        mode=cast(ApprovalMode, row.mode),
        revision=row.revision,
        updated_at=datetime.fromtimestamp(row.updated_at / 1000, UTC),
    )


def in_transaction(conn: Any, services: SharedServices, owner_id: int) -> TaskApprovalPolicy:
    row = services.user_task_approval_policy_repo.get(conn, owner_id)
    if row is None:
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    return view(row)


def current(services: SharedServices, owner_id: int) -> TaskApprovalPolicy:
    with services.db.read_snapshot() as conn:
        return in_transaction(conn, services, owner_id)


def snapshot(
    conn: Any, services: SharedServices, task_id: str, owner_id: int
) -> TaskApprovalPolicy:
    """Freeze the account policy inside the caller's owner-locked transaction."""
    if services.project_task_repo.get_in_transaction(conn, task_id, owner_id) is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return in_transaction(conn, services, owner_id)


def set_global_policy(
    services: SharedServices,
    owner_id: int,
    body: ApprovalPolicyUpdate,
    *,
    task_id: str | None = None,
) -> TaskApprovalPolicy:
    from octop.infra.projects.tasks import now_ms

    encoded = json.dumps(
        body.model_dump(exclude={"request_id"}), sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        if (
            task_id is not None
            and services.project_task_repo.get_in_transaction(conn, task_id, owner_id) is None
        ):
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        repo = services.user_task_approval_policy_repo
        row = repo.get(conn, owner_id)
        assert row is not None
        requests = json.loads(row.requests_json)
        previous = next((item for item in requests if item["request_id"] == body.request_id), None)
        if previous is not None:
            if previous["digest"] != digest:
                raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT, status=409)
        else:
            if row.revision != body.expected_revision:
                raise OctopError.localized(ErrorCode.TASK_APPROVAL_POLICY_CONFLICT, status=409)
            requests.append({"request_id": body.request_id, "digest": digest})
            row = UserTaskApprovalPolicyRow(
                owner_id, body.mode, row.revision + 1, now_ms(), json.dumps(requests)
            )
            repo.save(conn, row)
            services.project_task_repo.approval_changed(conn, owner_id)
        return view(row)


def set_policy(
    services: SharedServices, owner_id: int, task_id: str, body: ApprovalPolicyUpdate
) -> TaskDetail:
    from octop.infra.projects.tasks import detail

    set_global_policy(services, owner_id, body, task_id=task_id)
    return detail(services, owner_id, task_id)
