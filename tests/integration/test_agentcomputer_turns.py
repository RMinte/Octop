"""Guarded Turn admission and owned Run queries never dispatch a model."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from octop.api.routers import agentcomputer
from octop.infra.agentcomputer import submission
from octop.infra.agentcomputer.capabilities import CapabilityStatus, get_capabilities
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
)
from octop.infra.agentcomputer.identity import IdentityError
from octop.infra.agentcomputer.limits import MAX_COMMAND_BYTES
from octop.infra.agentcomputer.models import CommandReceipt, NewMessageCommand, RunView
from tests.support.agentcomputer import register_ready_task
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import create_user, resolve_user_id

PREFIX = "/api/agentcomputer/v1"
TABLES = (
    "ac_projects",
    "ac_tasks",
    "threads",
    "sessions",
    "ac_turns",
    "ac_runs",
    "ac_events",
    "ac_commands",
)


def command(**updates) -> dict:
    return {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "contextMode": "newMessage",
        "userMessageID": str(uuid4()),
        "expectedProjectRevision": 1,
        "prompt": "  Fixture 中文\ninput  ",
        "inputArtifactIDs": [],
        "selectedSkillIDs": [],
        "permissionMode": "askForApproval",
        **updates,
    }


def counts(server) -> dict[str, int]:
    assert server.services is not None
    with server.services.db.connect() as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES
        }


def available_profile(services):
    """Trusted test replacement only; there is no request/config override."""
    profile = get_capabilities(services)
    return profile.model_copy(
        update={
            "supportedPermissionModes": ["askForApproval"],
            "capabilities": profile.capabilities.model_copy(
                update={"runs": CapabilityStatus(supported=True, available=True)}
            ),
        }
    )


async def accepted_turn(env, monkeypatch):
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    task = await register_ready_task(client, server, auth, owner)
    assert server.app_runtime is not None
    no_start = AsyncMock(
        side_effect=AssertionError("control-plane admission must not start models")
    )
    monkeypatch.setattr(server.app_runtime.agent_registry, "_start_agent", no_start)
    monkeypatch.setattr(submission, "get_capabilities", available_profile)
    body = command()
    response = await client.post(f"{PREFIX}/tasks/{task.taskID}/turns", headers=auth, json=body)
    assert response.status_code == 201
    no_start.assert_not_awaited()
    return task, body, CommandReceipt.model_validate(response.json())


async def test_production_unavailable_admission_has_zero_business_writes(env) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    task = await register_ready_task(client, server, auth, owner)
    before = counts(server)
    response = await client.post(
        f"{PREFIX}/tasks/{task.taskID}/turns", headers=auth, json=command()
    )
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "runtimeUnavailable",
            "retryable": False,
            "reasonCode": "executionUnavailable",
        }
    }
    assert response.headers["cache-control"] == "no-store"
    assert counts(server) == before
    profile = (await client.get(f"{PREFIX}/capabilities", headers=auth)).json()
    assert profile["supportedPermissionModes"] == []
    assert profile["capabilities"]["runs"] == {
        "supported": True,
        "available": False,
        "reasonCode": "executionUnavailable",
    }


async def test_trusted_fixture_admission_is_durable_queued_without_dispatch(
    env, monkeypatch
) -> None:
    client, server, auth = env
    task, body, receipt = await accepted_turn(env, monkeypatch)
    assert receipt.taskID == task.taskID and receipt.runState == "queued"
    assert receipt.acceptedSequence == 1 and receipt.taskRevision == 2 and not receipt.replayed
    queried = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
    assert queried.json() == receipt.model_dump(mode="json")
    response = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    view = RunView.model_validate(response.json())
    assert view.runID == receipt.runID and view.turnID == receipt.turnID
    assert view.taskID == task.taskID and view.projectID == task.projectID
    assert view.state == "queued" and view.dispatchPhase == "notDispatched"
    assert view.executionEpoch == 0 and view.restoreEpoch == 1 and view.revision == 1
    assert not view.reconciliationRequired and view.createdAt == view.updatedAt
    assert view.createdAt.tzinfo == UTC
    assert set(response.json()) == set(RunView.model_fields)
    assert str(server.paths.root) not in response.text
    assert body["prompt"] not in response.text
    assert server.services is not None
    with server.services.db.connect() as conn:
        turn = conn.execute(
            "SELECT snapshot_json FROM ac_turns WHERE turn_id=?", (str(receipt.turnID),)
        ).fetchone()
        snapshot = json.loads(turn[0])
        assert snapshot["request"]["prompt"] == body["prompt"]
        assert snapshot["projectConfiguration"]["instructions"] == "Fixture only"
        assert (
            snapshot["request"]["inputArtifactIDs"] == snapshot["request"]["selectedSkillIDs"] == []
        )
        assert tuple(
            conn.execute(
                "SELECT enabled,last_state FROM agents WHERE managed_project_id=?",
                (str(task.projectID),),
            ).fetchone()
        ) == (0, "stopped")
    stored = counts(server)
    assert stored["ac_turns"] == stored["ac_runs"] == stored["ac_events"] == 1


async def test_replay_after_capability_closure_preserves_receipt_and_checks_epoch(
    env, monkeypatch
) -> None:
    client, server, auth = env
    task, body, receipt = await accepted_turn(env, monkeypatch)
    assert server.services is not None
    row = server.services.ac_command_repo.get(
        str(receipt.commandID), await resolve_user_id(client, auth, "admin")
    )
    assert row is not None
    original = row.receipt_json
    before = counts(server)
    monkeypatch.setattr(submission, "get_capabilities", get_capabilities)
    retry = {**body, "commandID": str(uuid4())}
    replay = await client.post(f"{PREFIX}/tasks/{task.taskID}/turns", headers=auth, json=retry)
    assert replay.status_code == 201
    assert replay.json() == receipt.model_copy(update={"replayed": True}).model_dump(mode="json")
    assert counts(server) == before
    assert (
        await client.get(f"{PREFIX}/commands/{retry['commandID']}", headers=auth)
    ).status_code == 404
    row = server.services.ac_command_repo.get(
        str(receipt.commandID), await resolve_user_id(client, auth, "admin")
    )
    assert row is not None and row.receipt_json == original
    path = server.paths.agentcomputer_identity
    identity = json.loads(path.read_text(encoding="utf-8"))
    identity["restoreEpoch"] = 2  # Persisted test metadata; not a recovery implementation.
    path.write_text(json.dumps(identity), encoding="utf-8")
    stale = await client.post(f"{PREFIX}/tasks/{task.taskID}/turns", headers=auth, json=body)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "restoreEpochConflict"
    assert counts(server) == before
    historical = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
    assert historical.status_code == 200 and historical.json()["restoreEpoch"] == 1


async def test_run_current_state_is_separate_from_original_queued_receipt(env, monkeypatch) -> None:
    client, server, auth = env
    _task, _body, receipt = await accepted_turn(env, monkeypatch)
    assert server.services is not None
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_runs SET state='interrupted', reconciliation_required=1, revision=2, updated_at=created_at+1 WHERE run_id=?",
            (str(receipt.runID),),
        )
    current = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
    assert current.status_code == 200
    assert current.json()["state"] == "interrupted"
    assert current.json()["reconciliationRequired"] is True and current.json()["revision"] == 2
    original = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
    assert original.status_code == 200 and original.json() == receipt.model_dump(mode="json")


async def test_turn_and_run_owner_isolation_includes_admin(env, monkeypatch) -> None:
    client, server, admin = env
    alice = await create_user(client, admin, username="turn-alice")
    bob = await create_user(client, admin, username="turn-bob")
    owner = await resolve_user_id(client, admin, "turn-alice")
    task = await register_ready_task(client, server, alice, owner)
    monkeypatch.setattr(submission, "get_capabilities", available_profile)
    first = await client.post(f"{PREFIX}/tasks/{task.taskID}/turns", headers=alice, json=command())
    assert first.status_code == 201
    receipt = CommandReceipt.model_validate(first.json())
    before = counts(server)
    for auth in (bob, admin):
        for task_id in (task.taskID, uuid4()):
            denied = await client.post(
                f"{PREFIX}/tasks/{task_id}/turns", headers=auth, json=command()
            )
            assert denied.status_code == 404
            assert denied.json() == {"error": {"code": "resourceNotFound", "retryable": False}}
        for run_id in (receipt.runID, uuid4()):
            denied = await client.get(f"{PREFIX}/runs/{run_id}", headers=auth)
            assert denied.status_code == 404
            assert denied.json() == {"error": {"code": "resourceNotFound", "retryable": False}}
    assert counts(server) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"inputArtifactIDs": [str(uuid4())]},
        {"selectedSkillIDs": ["unresolved-skill"]},
        {"permissionMode": "fullAccess"},
    ],
)
async def test_unimplemented_resource_or_permission_input_is_rejected(
    env, monkeypatch, changes
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    task = await register_ready_task(client, server, auth, owner)
    monkeypatch.setattr(submission, "get_capabilities", available_profile)
    before = counts(server)
    response = await client.post(
        f"{PREFIX}/tasks/{task.taskID}/turns", headers=auth, json=command(**changes)
    )
    assert (
        response.status_code == 422 and response.json()["error"]["code"] == "capabilityUnsupported"
    )
    if "permissionMode" in changes:
        assert response.json()["error"]["reasonCode"] == "permissionModeUnsupported"
    assert counts(server) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"contextMode": "continueOriginalMessage"},
        {"expectedProjectRevision": True},
        {"expectedRestoreEpoch": "1"},
        {"ownerID": "private-input-marker"},
        {"inputArtifactIDs": ["private-input-marker"]},
        {"selectedSkillIDs": [42]},
        {"prompt": 42},
        {"idempotencyKey": "x" * 257},
    ],
)
async def test_turn_schema_is_strict_and_validation_does_not_echo_input(env, changes) -> None:
    client, server, auth = env
    before = counts(server)
    response = await client.post(
        f"{PREFIX}/tasks/{uuid4()}/turns", headers=auth, json=command(**changes)
    )
    assert response.status_code == 422
    assert response.json() == {"error": {"code": "invalidRequest", "retryable": False}}
    assert "private-input-marker" not in response.text
    assert counts(server) == before


async def test_turn_and_run_uuid_validation_is_scoped(env) -> None:
    client, _server, auth = env
    for response in (
        await client.post(
            f"{PREFIX}/tasks/private-input-marker/turns", headers=auth, json=command()
        ),
        await client.get(f"{PREFIX}/runs/private-input-marker", headers=auth),
    ):
        assert response.status_code == 422
        assert response.json() == {"error": {"code": "invalidRequest", "retryable": False}}
        assert "private-input-marker" not in response.text


async def test_turn_and_run_require_header_authentication(env) -> None:
    client, server, auth = env
    token = auth["Authorization"].split(" ", 1)[1]
    for headers, params in (
        ({}, {}),
        ({}, {"access_token": token}),
        (auth, {"access_token": token}),
    ):
        responses = (
            await client.post(
                f"{PREFIX}/tasks/{uuid4()}/turns", headers=headers, params=params, json=command()
            ),
            await client.get(f"{PREFIX}/runs/{uuid4()}", headers=headers, params=params),
        )
        for response in responses:
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "authenticationRequired"
            assert response.headers["cache-control"] == "no-store"
    assert not server.paths.agentcomputer_identity.exists()


async def chunks(body: bytes):
    for offset in range(0, len(body), 1024):
        yield body[offset : offset + 1024]


@pytest.mark.parametrize("content_length", [None, "1"])
async def test_turn_body_limit_counts_streamed_bytes_before_domain(
    env, monkeypatch, content_length
) -> None:
    client, server, auth = env
    operation = Mock(side_effect=AssertionError("oversized input reached domain"))
    monkeypatch.setattr(agentcomputer, "submit_new_message", operation)
    headers = {**auth, "content-type": "application/json"}
    if content_length is not None:
        headers["content-length"] = content_length
    response = await client.post(
        f"{PREFIX}/tasks/{uuid4()}/turns",
        headers=headers,
        content=chunks(b"x" * (MAX_COMMAND_BYTES + 1)),
    )
    assert response.status_code == 413
    assert response.json() == {"error": {"code": "payloadLimitExceeded", "retryable": False}}
    operation.assert_not_called()
    assert counts(server)["ac_runs"] == 0


async def test_exact_command_limit_is_parsed_without_truncation_and_auth_runs_first(
    env, monkeypatch
) -> None:
    client, _server, auth = env

    def refused(*args, **kwargs):
        raise CommandAdmissionError("runtimeUnavailable", reason="executionUnavailable")

    operation = Mock(side_effect=refused)
    monkeypatch.setattr(agentcomputer, "submit_new_message", operation)
    body = command(prompt="")
    empty = json.dumps(body, separators=(",", ":")).encode()
    body["prompt"] = "x" * (MAX_COMMAND_BYTES - len(empty))
    encoded = json.dumps(body, separators=(",", ":")).encode()
    assert len(encoded) == MAX_COMMAND_BYTES
    response = await client.post(
        f"{PREFIX}/tasks/{uuid4()}/turns",
        headers={**auth, "content-type": "application/json"},
        content=chunks(encoded),
    )
    assert response.status_code == 503
    assert operation.call_args.kwargs["command"].prompt == body["prompt"]
    operation.reset_mock()
    anonymous = await client.post(f"{PREFIX}/tasks/{uuid4()}/turns", content=chunks(encoded + b"x"))
    assert anonymous.status_code == 401
    operation.assert_not_called()


@pytest.mark.parametrize("endpoint", ["turn", "run"])
@pytest.mark.parametrize(
    "failure", [IdentityError, StorageDataError, StorageUnavailableError, OSError]
)
async def test_turn_and_run_storage_failures_are_safe(env, monkeypatch, endpoint, failure) -> None:
    client, _server, auth = env

    def unavailable(*args, **kwargs):
        raise failure("private-storage-marker")

    monkeypatch.setattr(
        agentcomputer, "submit_new_message" if endpoint == "turn" else "get_run", unavailable
    )
    response = (
        await client.post(f"{PREFIX}/tasks/{uuid4()}/turns", headers=auth, json=command())
        if endpoint == "turn"
        else await client.get(f"{PREFIX}/runs/{uuid4()}", headers=auth)
    )
    assert response.status_code == 503 and response.json()["error"]["code"] == "storageUnavailable"
    assert response.headers["cache-control"] == "no-store"
    assert "private-storage-marker" not in response.text
    if failure is IdentityError:
        assert response.json()["error"]["reasonCode"] == "identityInvalid"
    elif failure is StorageDataError:
        assert response.json()["error"]["reasonCode"] == (
            "submissionInvalid" if endpoint == "turn" else "runInvalid"
        )
    else:
        assert response.json()["error"]["retryable"] is True


async def test_broken_run_snapshot_is_rejected_without_repair(env, monkeypatch) -> None:
    client, server, auth = env
    _task, _body, receipt = await accepted_turn(env, monkeypatch)
    assert server.services is not None
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_turns SET snapshot_digest='sha256:private-digest-marker' WHERE turn_id=?",
            (str(receipt.turnID),),
        )
    response = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": "runInvalid"}
    }
    assert "private-digest-marker" not in response.text
    with server.services.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT snapshot_digest FROM ac_turns WHERE turn_id=?", (str(receipt.turnID),)
            ).fetchone()[0]
            == "sha256:private-digest-marker"
        )


@pytest.mark.parametrize("payload", [None, [], {"commandID": 1}])
async def test_corrupt_run_acceptance_payload_returns_safe_503(env, monkeypatch, payload) -> None:
    client, server, auth = env
    _task, _body, receipt = await accepted_turn(env, monkeypatch)
    assert server.services is not None
    damaged = json.dumps(payload)
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_events SET payload_json=? WHERE run_id=? AND event_type='run.accepted'",
            (damaged, str(receipt.runID)),
        )
    response = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": "runInvalid"}
    }
    with server.services.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT payload_json FROM ac_events WHERE run_id=?", (str(receipt.runID),)
            ).fetchone()[0]
            == damaged
        )


async def test_malformed_persisted_run_number_returns_safe_storage_errors(env, monkeypatch) -> None:
    client, server, auth = env
    task, body, receipt = await accepted_turn(env, monkeypatch)
    assert server.services is not None
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_runs SET execution_epoch='invalid-number' WHERE run_id=?",
            (str(receipt.runID),),
        )
    before = counts(server)
    responses = (
        (await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth), "runInvalid"),
        (
            await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth),
            "receiptInvalid",
        ),
        (
            await client.post(f"{PREFIX}/tasks/{task.taskID}/turns", headers=auth, json=body),
            "submissionInvalid",
        ),
    )
    for response, reason in responses:
        assert response.status_code == 503
        assert response.json() == {
            "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": reason}
        }
        assert "invalid-number" not in response.text
    assert counts(server) == before


async def test_openapi_documents_guarded_registration_and_current_run_view(tmp_path: Path) -> None:
    home = tmp_path / "control"
    write_octop_config(home, enable_api_docs=True)
    async with octop_client(home) as (client, _server):
        schema = (await client.get("/api/openapi.json")).json()
        turn = schema["paths"][f"{PREFIX}/tasks/{{taskID}}/turns"]["post"]
        run = schema["paths"][f"{PREFIX}/runs/{{runID}}"]
        assert set(run) == {"get"}
        for operation in (turn, run["get"]):
            assert operation["tags"] == ["agentcomputer"]
            assert operation["security"] == [{"BearerAuth": []}]
            assert operation["summary"]
        assert "executionUnavailable" in turn["description"]
        body = turn["requestBody"]["content"]["application/json"]
        assert body["schema"]["$ref"].endswith("/NewMessageCommand")
        assert NewMessageCommand.model_validate(body["example"]).contextMode == "newMessage"
        response = turn["responses"]["201"]["content"]["application/json"]
        assert response["schema"]["$ref"].endswith("/CommandReceipt")
        assert CommandReceipt.model_validate(response["example"]).runState == "queued"
        assert "202" not in turn["responses"]
        current = run["get"]["responses"]["200"]["content"]["application/json"]
        assert current["schema"]["$ref"].endswith("/RunView")
        assert "example" not in current
        assert (
            RunView.model_validate(current["examples"]["notDispatched"]["value"]).executionEpoch
            == 0
        )
        assert set(current["examples"]) == {
            "notDispatched",
            "claimed",
            "dispatching",
            "engineAccepted",
            "cancelRequested",
            "interrupted",
        }
        for example in current["examples"].values():
            RunView.model_validate(example["value"])
        for status in ("401", "403", "404", "409", "413", "422", "503"):
            assert turn["responses"][status]["content"]["application/json"]["schema"][
                "$ref"
            ].endswith("/ProtocolErrorEnvelope")
        assert (await client.get("/api/docs")).status_code == 200
