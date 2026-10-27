"""Freeze action policy and apply isolated review results to the original interrupt."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from octop.infra.db.repos.project_task_reviews import TaskReviewRow
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.projects import approval_policy, interactions
from octop.infra.projects.interaction_models import (
    ApprovalPayload,
    InteractionResolution,
    TaskInteraction,
)
from octop.infra.projects.review_models import ApprovalReview
from octop.infra.utils.ulid import new_ulid

# Only command contracts are governed by the three modes in this release.
MODE_TOOLS = frozenset({"execute"})


def review_view(row: TaskReviewRow) -> ApprovalReview:
    proposal = json.loads(row.proposal_json)
    return ApprovalReview.model_validate(
        {
            "review_id": row.review_id,
            "status": row.status,
            "decision": row.decision or None,
            "reason_code": row.reason_code,
            "action_digest": row.action_digest,
            "context_digest": proposal.get("context_digest"),
            "model_ref": row.model_ref,
            "created_at": datetime.fromtimestamp(row.created_at / 1000, UTC),
            "updated_at": datetime.fromtimestamp(row.updated_at / 1000, UTC),
            "expires_at": datetime.fromtimestamp(row.expires_at / 1000, UTC),
        }
    )


def _resolve(item: TaskInteraction, decision: str, source: str, now: int) -> None:
    item.status = "resolved"
    item.resolution = InteractionResolution.model_validate({"outcome": decision, "source": source})
    item.resolved_at = datetime.fromtimestamp(now / 1000, UTC)
    item.revision += 1


def freeze_server(
    services: SharedServices,
    conn: Any,
    owner: int,
    turn: ProjectTaskTurnRow,
    item: TaskInteraction,
    previous: list[TaskInteraction],
    now: int,
) -> None:
    payload = item.payload
    assert isinstance(payload, ApprovalPayload)
    policy = approval_policy.snapshot(conn, services, turn.task_id, owner)
    from octop.infra.projects.source_access import cron_skip_confirmation, external_unverified

    if external_unverified(json.loads(turn.configuration_json)):
        policy = policy.model_copy(update={"mode": "ask_for_approval"})
    settings = approval_policy.settings(services)
    payload.approval_mode = policy.mode
    payload.approval_revision = policy.revision
    payload.action_digest = interactions.digest(
        {"proposal": payload.action_digest, "policy": policy.model_dump(mode="json")}
    )
    payload.action_signature = interactions.digest(
        {
            "host": "server",
            "tool": payload.tool_name,
            "arguments": payload.arguments,
            "configuration": turn.configuration_json,
        }
    )
    if any(
        isinstance(old.payload, ApprovalPayload)
        and old.payload.action_signature == payload.action_signature
        and old.resolution is not None
        and old.resolution.outcome == "deny"
        and not (old.resolution.source == "timeout" and item.supersedes_interaction_id)
        for old in previous
    ):
        _resolve(item, "deny", "previous_denial", now)
        return
    if (
        payload.tool_name in settings.require_human_tools
        or "allow_once" not in payload.allowed_decisions
    ):
        return
    if cron_skip_confirmation(services, conn, owner, turn):
        _resolve(item, "allow_once", "cron_skip_confirmation", now)
        return
    if payload.tool_name not in MODE_TOOLS:
        return
    if policy.mode == "full_access":
        _resolve(item, "allow_once", "full_access_mode", now)
    elif policy.mode == "approve_for_me":
        configuration = json.loads(turn.configuration_json)
        workspace = configuration.get("directory") or str(
            services.paths.root / "project-tasks" / turn.task_id / "workspace"
        )
        proposal = interactions.encode(
            {
                "execution_host": "server",
                "tool_name": payload.tool_name,
                "arguments": payload.arguments,
                "action_digest": payload.action_digest,
                "working_directory": workspace,
                "execution_profile": "host_process",
                "mode": policy.mode,
                "approval_revision": policy.revision,
                "user_goal": turn.prompt[:4000],
            }
        )
        row = services.project_task_review_repo.enqueue(
            conn,
            TaskReviewRow(
                review_id="review_" + new_ulid(),
                task_id=turn.task_id,
                turn_id=turn.turn_id,
                owner_id=owner,
                execution_host="server",
                target_id=item.interaction_id,
                action_digest=payload.action_digest,
                proposal_json=proposal,
                model_ref=turn.model_ref,
                created_at=now,
                updated_at=now,
                expires_at=int(item.expires_at.timestamp() * 1000),
            ),
        )
        item.status = "reviewing"
        item.review = review_view(row)


def rearm_expired(
    services: SharedServices,
    conn: Any,
    owner: int,
    turn: ProjectTaskTurnRow,
    items: list[TaskInteraction],
    wait: dict[str, Any],
    now: int,
) -> bool:
    """Renew unexecuted timeout approvals only after an explicit interrupted resume."""
    changed = interactions.expire(items)
    by_id = {item.interaction_id: item for item in items}
    for group in wait.get("groups", []):
        for action in group["actions"]:
            if action["type"] != "approval":
                continue
            old = by_id[action["id"]]
            if (
                old.status != "expired"
                or old.dispatch_started
                or old.resolution is None
                or old.resolution.outcome != "deny"
                or old.resolution.source != "timeout"
                or not isinstance(old.payload, ApprovalPayload)
            ):
                continue
            payload = old.payload.model_copy(deep=True)
            payload.action_digest = interactions.digest(
                {
                    "supersedes_interaction_id": old.interaction_id,
                    "tool_call_id": old.tool_call_id,
                    "tool_name": payload.tool_name,
                    "arguments": payload.arguments,
                    "configuration": turn.configuration_json,
                }
            )
            fresh = interactions.new(turn, payload, old.tool_call_id)
            fresh.supersedes_interaction_id = old.interaction_id
            fresh.created_at = datetime.fromtimestamp(now / 1000, UTC)
            fresh.expires_at = fresh.created_at + timedelta(seconds=interactions.WAIT_SECONDS)
            freeze_server(services, conn, owner, turn, fresh, items, now)
            items.append(fresh)
            by_id[fresh.interaction_id] = fresh
            action["id"] = fresh.interaction_id
            changed = True
    return changed


def advance(
    services: SharedServices,
    conn: Any,
    turn: ProjectTaskTurnRow,
    items: list[TaskInteraction],
    now: int,
) -> bool:
    changed = interactions.expire(items)
    settings = approval_policy.settings(services)
    for item in items:
        if item.status != "reviewing":
            continue
        row = services.project_task_review_repo.get(conn, "server", item.interaction_id)
        if row is None:
            continue
        latest = review_view(row)
        if item.review != latest:
            item.review = latest
            item.revision += 1
            changed = True
        if row.status not in {"completed", "cancelled"}:
            continue
        payload = item.payload
        assert isinstance(payload, ApprovalPayload)
        from octop.infra.projects.source_access import external_unverified

        allowed = payload.tool_name not in settings.require_human_tools and not external_unverified(
            json.loads(turn.configuration_json)
        )
        if row.action_digest != payload.action_digest:
            item.status = "pending"
            item.revision += 1
        elif row.status == "completed" and row.decision in {"allow_once", "deny"} and allowed:
            _resolve(item, row.decision, "auto_reviewer", now)
        else:
            item.status = "pending"
            item.revision += 1
        changed = True
    if changed:
        services.project_task_turn_repo.save_interactions(
            conn, turn.turn_id, interactions.dump(items), now
        )
    return changed


def wait_kind(items: list[TaskInteraction], *, clients: bool, user_paused: bool) -> str:
    if user_paused:
        return "user"
    if any(i.kind == "approval" and i.status == "pending" for i in items):
        return "approval"
    if any(i.kind == "question" and i.status == "pending" for i in items):
        return "answer"
    if clients:
        return "client_tool"
    return "review" if any(i.status == "reviewing" for i in items) else ""
