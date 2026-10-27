"""A Cron occurrence grants one turn server confirmations, never account authority."""
# ruff: noqa: F811

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from tests.unit.projects.test_resume_receipts import services  # noqa: F401

from octop.infra.db.repos.unified_sources import (
    CronOccurrenceRepo,
    UnifiedSourceInboxRepo,
    UnifiedSourceRouteRepo,
)
from octop.infra.projects import approval_policy, approval_runtime, interactions
from octop.infra.projects.control import TaskControlMiddleware
from octop.infra.projects.ingress import SourceEnvelope
from octop.infra.projects.interaction_models import ApprovalPayload, InteractionResolution


def accepted_cron(services, *, skip=True, authority_skip=None):
    services.cron_repo.create(
        cron_id="cron_one",
        agent_id=None,
        user_id=1,
        trigger="0 9 * * *",
        prompt="Scheduled task",
        session_key="dashboard:1",
        skip_confirmation=skip is True,
    )
    source = SourceEnvelope(
        source_kind="cron",
        source_instance="server_one",
        source_key="cron:cron_one",
        event_id="occurrence_one",
        authority={
            "cron_id": "cron_one",
            "automatic": True,
            "skip_confirmation": skip if authority_skip is None else authority_skip,
        },
    )
    with services.db.transaction() as conn:
        route = UnifiedSourceRouteRepo.ensure(
            conn,
            owner_id=1,
            source_kind="cron",
            source_instance=source.source_instance,
            source_key=source.source_key,
            now=1,
        )
        UnifiedSourceRouteRepo.transition(
            conn, 1, route.route_id, route.revision, "task", "task", task_id="task_one", now=1
        )
        inbox = UnifiedSourceInboxRepo.receive(
            conn,
            inbox_id="input_cron",
            route_id=route.route_id,
            owner_id=1,
            event_id=source.event_id,
            fingerprint="cron-fingerprint",
            payload_json=json.dumps({"source": source.model_dump(mode="json")}),
            now=1,
        )
        UnifiedSourceInboxRepo.admit(conn, inbox, "task_one", "turn_one", 1)
        CronOccurrenceRepo.receive(
            conn,
            occurrence_id=source.event_id,
            cron_id="cron_one",
            owner_id=1,
            event_key="schedule:one",
            payload_json=json.dumps(
                {
                    "cron_id": "cron_one",
                    "user_id": 1,
                    "event_key": "schedule:one",
                    "task_type": "agent",
                    "skip_confirmation": skip,
                }
            ),
            now=1,
        )
        turn = services.project_task_turn_repo.get_in_transaction(conn, "turn_one")
        configuration = json.loads(turn.configuration_json)
        configuration["source_origin"] = {
            "inbox_id": inbox.inbox_id,
            "route_id": route.route_id,
            "source": source.model_dump(mode="json"),
        }
        conn.execute(
            "UPDATE project_task_turns SET configuration_json=? WHERE turn_id='turn_one'",
            (json.dumps(configuration),),
        )
    return services.project_task_turn_repo.get("turn_one")


def freeze(services, turn, *, name="execute", decisions=None, previous=None):
    item = interactions.new(
        turn,
        ApprovalPayload(
            tool_name=name,
            arguments={"value": "original proposal"},
            reason="Confirm this action",
            action_digest="original-proposal",
            allowed_decisions=["allow_once", "deny"] if decisions is None else decisions,
        ),
        "call_one",
    )
    with services.db.transaction() as conn:
        approval_runtime.freeze_server(services, conn, 1, turn, item, previous or [], 1)
    return item


@pytest.mark.parametrize("name", ["execute", "mcp__mail__send", "publish_site"])
def test_accepted_cron_skips_server_confirmation_with_distinct_provenance(services, name):
    turn = accepted_cron(services)
    original_policy = approval_policy.current(services, 1)
    item = freeze(services, turn, name=name)
    assert item.status == "resolved"
    assert item.resolution == InteractionResolution(
        outcome="allow_once", source="cron_skip_confirmation"
    )
    assert item.payload.approval_mode == original_policy.mode == "ask_for_approval"
    assert item.payload.approval_revision == original_policy.revision
    assert approval_policy.current(services, 1) == original_policy
    assert interactions.load(replace(turn, interactions_json=interactions.dump([item]))) == [item]
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM project_task_reviews").fetchone()["n"] == 0


@pytest.mark.parametrize("skip", [False, None, "true", 1])
def test_occurrence_requires_explicit_boolean_skip(services, skip):
    turn = accepted_cron(services, skip=skip, authority_skip=True)
    item = freeze(services, turn, name="mcp__mail__send")
    assert item.status == "pending" and item.resolution is None


@pytest.mark.parametrize("flag", [False, None, "true", 1])
def test_source_authority_also_requires_the_frozen_boolean(services, flag):
    turn = accepted_cron(services, authority_skip=flag)
    if flag is None:
        configuration = json.loads(turn.configuration_json)
        configuration["source_origin"]["source"]["authority"].pop("skip_confirmation")
        turn = replace(turn, configuration_json=json.dumps(configuration))
    item = freeze(services, turn)
    assert item.status == "pending" and item.resolution is None


def test_editing_a_job_does_not_rewrite_the_accepted_occurrence_choice(services):
    turn = accepted_cron(services)
    services.cron_repo.update("cron_one", skip_confirmation=False)
    item = freeze(services, turn)
    assert item.resolution == InteractionResolution(
        outcome="allow_once", source="cron_skip_confirmation"
    )


@pytest.mark.parametrize("protection", ["mandatory_human", "security_deny", "previous_denial"])
def test_cron_skip_preserves_existing_server_restrictions(services, protection):
    turn = accepted_cron(services)
    if protection == "mandatory_human":
        services.settings_repo.set("task_approval_policy", '{"require_human_tools":["execute"]}')
    previous = []
    if protection == "previous_denial":
        denied = freeze(services, turn)
        denied.resolution = InteractionResolution(outcome="deny", source="user")
        previous.append(denied)
    item = freeze(
        services,
        turn,
        decisions=["deny"] if protection == "security_deny" else None,
        previous=previous,
    )
    if protection == "previous_denial":
        assert item.resolution == InteractionResolution(outcome="deny", source="previous_denial")
    else:
        assert item.status == "pending" and item.resolution is None


@pytest.mark.parametrize(
    "tamper",
    ["source", "inbox_turn", "inbox_owner", "occurrence_owner", "occurrence_binding", "acp"],
)
def test_a_task_cannot_borrow_or_invent_a_cron_grant(services, tamper):
    turn = accepted_cron(services)
    configuration = json.loads(turn.configuration_json)
    with services.db.transaction() as conn:
        if tamper == "source":
            configuration["source_origin"]["source"]["prompt"] = "forged source"
        elif tamper == "inbox_turn":
            conn.execute("UPDATE unified_source_inbox SET turn_id=NULL WHERE inbox_id='input_cron'")
        elif tamper == "inbox_owner":
            conn.execute(
                "INSERT INTO users(id,username,role,created_at) VALUES(2,'other','user',1)"
            )
            conn.execute("UPDATE unified_source_inbox SET owner_id=2 WHERE inbox_id='input_cron'")
        elif tamper == "occurrence_owner":
            conn.execute(
                "INSERT INTO users(id,username,role,created_at) VALUES(2,'other','user',1)"
            )
            conn.execute("UPDATE unified_cron_occurrences SET owner_id=2")
        elif tamper == "occurrence_binding":
            services.project_task_turn_repo.insert(
                conn, replace(turn, turn_id="another_turn", request_id="another_request")
            )
            conn.execute("UPDATE unified_cron_occurrences SET turn_id='another_turn'")
        else:
            configuration["execution_engine"] = {"engine_id": "acp"}
    item = freeze(
        services, replace(turn, configuration_json=json.dumps(configuration)), name="publish_site"
    )
    assert item.status == "pending" and item.resolution is None


def test_normal_followup_and_direct_task_flag_do_not_inherit_cron_skip(services):
    turn = accepted_cron(services)
    configuration = json.loads(turn.configuration_json)
    configuration.pop("source_origin")
    configuration["skip_confirmation"] = True
    item = freeze(services, replace(turn, configuration_json=json.dumps(configuration)))
    assert item.status == "pending" and item.resolution is None


@pytest.mark.parametrize("revocation", ["mandatory_human", "disabled_cron", "blocked_route"])
def test_dispatch_rechecks_revocation_before_using_a_cron_approval(services, revocation):
    turn = accepted_cron(services)
    item = freeze(services, turn)
    with services.db.transaction() as conn:
        services.project_task_turn_repo.save_interactions(
            conn, turn.turn_id, interactions.dump([item]), 1
        )
    if revocation == "mandatory_human":
        services.settings_repo.set("task_approval_policy", '{"require_human_tools":["execute"]}')
    elif revocation == "disabled_cron":
        services.cron_repo.update("cron_one", enabled=False)
    else:
        with services.db.transaction() as conn:
            conn.execute("UPDATE unified_source_routes SET state='blocked'")
    middleware = TaskControlMiddleware(services, 1, turn, "en")
    assert not middleware._authorize(
        {"id": item.tool_call_id, "name": "execute", "args": item.payload.arguments}
    )
    current = services.project_task_turn_repo.get(turn.turn_id)
    assert not interactions.load(current)[0].dispatch_started


def test_cron_approval_authorizes_only_the_exact_action_once(services):
    turn = accepted_cron(services)
    item = freeze(services, turn, name="mcp__mail__send")
    with services.db.transaction() as conn:
        services.project_task_turn_repo.save_interactions(
            conn, turn.turn_id, interactions.dump([item]), 1
        )
    middleware = TaskControlMiddleware(services, 1, turn, "en")
    call = {"id": item.tool_call_id, "name": item.payload.tool_name, "args": item.payload.arguments}
    assert not middleware._authorize({**call, "args": {"value": "different proposal"}})
    assert middleware._authorize(call)
    assert not middleware._authorize(call)
