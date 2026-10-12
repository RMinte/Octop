"""Malformed stored numeric facts fail safely without coercion or repair."""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.support.agentcomputer import register_ready_task
from tests.support.auth import resolve_user_id

PREFIX = "/api/agentcomputer/v1"


def project_command() -> dict:
    return {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "projectID": str(uuid4()),
        "name": "Stored numeric fixture",
        "configuration": {
            "schemaVersion": 1,
            "modelRef": "not-configured/no-model",
            "instructions": "Fixture only",
        },
    }


def turn_command() -> dict:
    return {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "contextMode": "newMessage",
        "userMessageID": str(uuid4()),
        "expectedProjectRevision": 1,
        "prompt": "Do not register a Run from damaged facts",
        "permissionMode": "askForApproval",
    }


def control_facts(services) -> dict:
    with services.db.connect() as conn:
        return {
            table: [
                tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            ]
            for table in (
                "ac_projects",
                "ac_project_preparations",
                "ac_tasks",
                "ac_commands",
                "ac_turns",
                "ac_runs",
                "ac_events",
                "agents",
            )
        }


def assert_safe_storage_failure(response, reason: str) -> None:
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": reason}
    }
    assert "private-numeric-marker" not in response.text
    assert "Stored value" not in response.text


@pytest.mark.parametrize("resource", ["project", "preparation", "task"])
@pytest.mark.parametrize("value", [True, 1.5, "private-numeric-marker"])
async def test_public_reads_and_registration_reject_raw_numeric_coercion(
    env, monkeypatch, resource, value
) -> None:
    client, server, auth = env
    assert server.services is not None
    services = server.services
    owner = await resolve_user_id(client, auth, "admin")
    if resource == "task":
        task = await register_ready_task(client, server, auth, owner)
        resource_id = str(task.taskID)
        table, id_column = "ac_tasks", "task_id"
        original = services.ac_task_repo.get(resource_id, owner)
        endpoint = f"{PREFIX}/tasks/{resource_id}"
        write_endpoint, body = f"{endpoint}/turns", turn_command()
        read_reason, write_reason = "taskInvalid", "submissionInvalid"
    else:
        body = project_command()
        response = await client.post(f"{PREFIX}/projects", headers=auth, json=body)
        assert response.status_code == 201
        resource_id = body["projectID"]
        endpoint = f"{PREFIX}/projects/{resource_id}"
        write_endpoint = f"{PREFIX}/projects"
        read_reason = write_reason = "projectInvalid"
        if resource == "project":
            table, id_column = "ac_projects", "project_id"
            original = services.ac_project_repo.get(resource_id, owner)
        else:
            table, id_column = "ac_project_preparations", "project_id"
            original = services.ac_project_preparation_repo.get(resource_id, owner)
    assert original is not None
    if type(value) is bool:
        # SQLite normalizes bound bools to INTEGER 0/1. Simulate a driver returning
        # an actual raw bool, while preserving the real repo decoder and HTTP path.
        row_type = type(original)
        decode = row_type.from_row

        def raw_bool(cls, row):
            fields = dict(row)
            fields["revision"] = value
            return decode(fields)

        monkeypatch.setattr(row_type, "from_row", classmethod(raw_bool))
    else:
        # INTEGER affinity permits a fractional REAL or nonnumeric TEXT even
        # when the existing positive CHECK passes; persist these in the test DB.
        with services.db.transaction() as conn:
            conn.execute(f"UPDATE {table} SET revision=? WHERE {id_column}=?", (value, resource_id))
        with services.db.connect() as conn:
            stored = conn.execute(
                f"SELECT revision FROM {table} WHERE {id_column}=?", (resource_id,)
            ).fetchone()[0]
            assert type(stored) is type(value) and stored == value
    damaged_facts = control_facts(services)
    read = await client.get(endpoint, headers=auth)
    assert_safe_storage_failure(read, read_reason)
    registration = await client.post(write_endpoint, headers=auth, json=body)
    assert_safe_storage_failure(registration, write_reason)
    assert control_facts(services) == damaged_facts


async def test_unknown_preparation_schema_is_rejected_by_project_get_and_retry(env) -> None:
    client, server, auth = env
    assert server.services is not None
    services = server.services
    body = project_command()
    created = await client.post(f"{PREFIX}/projects", headers=auth, json=body)
    assert created.status_code == 201
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_project_preparations SET schema_version=2 WHERE project_id=?",
            (body["projectID"],),
        )
    damaged_facts = control_facts(services)
    assert_safe_storage_failure(
        await client.get(f"{PREFIX}/projects/{body['projectID']}", headers=auth), "projectInvalid"
    )
    assert_safe_storage_failure(
        await client.post(f"{PREFIX}/projects", headers=auth, json=body), "projectInvalid"
    )
    assert control_facts(services) == damaged_facts
    with services.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT schema_version FROM ac_project_preparations WHERE project_id=?",
                (body["projectID"],),
            ).fetchone()[0]
            == 2
        )


async def test_unknown_preparation_schema_cannot_back_task_read_or_turn_registration(env) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    task = await register_ready_task(client, server, auth, owner)
    assert server.services is not None
    services = server.services
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_project_preparations SET schema_version=2 WHERE project_id=?",
            (str(task.projectID),),
        )
    damaged_facts = control_facts(services)
    assert_safe_storage_failure(
        await client.get(f"{PREFIX}/tasks/{task.taskID}", headers=auth), "taskInvalid"
    )
    assert_safe_storage_failure(
        await client.post(f"{PREFIX}/tasks/{task.taskID}/turns", headers=auth, json=turn_command()),
        "submissionInvalid",
    )
    assert control_facts(services) == damaged_facts
    with services.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT schema_version FROM ac_project_preparations WHERE project_id=?",
                (str(task.projectID),),
            ).fetchone()[0]
            == 2
        )
