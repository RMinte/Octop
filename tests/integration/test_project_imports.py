"""External projects and individually selectable configuration stay owner scoped and inert."""

from __future__ import annotations

import base64
from uuid import uuid4

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.projects import import_models
from tests.support.auth import create_user


def project_body(**changes):
    return {
        "source": "codex",
        "external_project_id": "external-project",
        "name": "Imported project",
        "client_directory": "C:\\Users\\fixture\\project",
        **changes,
    }


def configuration_body(**changes):
    return {
        "source": "codex",
        "category": "skills",
        "name": "Review skill",
        "source_path": "/client-only/.codex/skills/review",
        "files": [
            {
                "relative_path": "SKILL.md",
                "content_base64": base64.b64encode(b"Review carefully").decode(),
            }
        ],
        **changes,
    }


async def test_project_import_creates_unprivileged_project_and_preserves_later_edits(env):
    client, server, auth = env
    agents = server.services.agent_repo.list_all()
    created = await client.post("/api/projects/import", headers=auth, json=project_body())
    assert created.status_code == 200, created.text
    assert created.json()["action"] == "imported"
    project = created.json()["project"]
    assert project["directory"] is None
    assert project["client_directory"] == project_body()["client_directory"]
    assert project["instructions"] == "" and project["skills"] == project["mcp_servers"] == []
    changed = await client.patch(
        f"/api/projects/{project['project_id']}",
        headers=auth,
        json={"expected_revision": 1, "name": "My edited name", "instructions": "Keep my edits"},
    )
    assert changed.status_code == 200, changed.text
    repeated = await client.post(
        "/api/projects/import",
        headers=auth,
        json=project_body(name="New source name", client_directory="/client/changed"),
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {"action": "unchanged", "project": changed.json()}
    assert server.services.agent_repo.list_all() == agents
    assert created.headers["cache-control"] == "no-store"


async def test_project_import_does_not_claim_same_name_and_explicit_link_is_owner_scoped(env):
    client, _server, auth = env
    original = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Imported project"},
        )
    ).json()
    imported = (
        await client.post("/api/projects/import", headers=auth, json=project_body())
    ).json()["project"]
    assert imported["project_id"] != original["project_id"]
    assert imported["name"].startswith("Imported project (codex-")
    linked = await client.post(
        "/api/projects/import",
        headers=auth,
        json=project_body(external_project_id="explicit-link", project_id=original["project_id"]),
    )
    assert linked.status_code == 200 and linked.json()["action"] == "linked"
    assert linked.json()["project"] == original
    conflict = await client.post(
        "/api/projects/import",
        headers=auth,
        json=project_body(project_id=original["project_id"]),
    )
    assert conflict.status_code == 409
    alice = await create_user(client, auth, username="alice")
    denied = await client.post(
        "/api/projects/import",
        headers=alice,
        json=project_body(project_id=original["project_id"]),
    )
    assert denied.status_code == 404
    own = await client.post("/api/projects/import", headers=alice, json=project_body())
    assert own.status_code == 200 and own.json()["action"] == "imported"
    assert own.json()["project"]["project_id"] != imported["project_id"]


async def test_project_import_validates_client_path_without_reading_it_and_has_no_server_path(env):
    client, _server, auth = env
    invalid = await client.post(
        "/api/projects/import", headers=auth, json=project_body(client_directory="relative")
    )
    assert invalid.status_code == 422
    forbidden_field = await client.post(
        "/api/projects/import", headers=auth, json=project_body(directory="/host/path")
    )
    assert forbidden_field.status_code == 422
    assert (await client.post("/api/projects/import", json=project_body())).status_code == 401


async def test_configuration_entries_are_independent_filterable_and_never_activated(env):
    client, server, auth = env
    before_config = server.paths.config.read_bytes()
    before_agents = server.services.agent_repo.list_all()
    ids = set()
    for category in ("settings", "instructions", "plugins", "skills", "mcpServers"):
        response = await client.post(
            "/api/projects/import/configurations",
            headers=auth,
            json=configuration_body(category=category, name=f"Selected {category}"),
        )
        assert response.status_code == 200, response.text
        item = response.json()["configuration"]
        assert response.json()["action"] == "imported"
        assert item["status"] == "pendingActivation" and item["file_count"] == 1
        ids.add(item["configuration_id"])
        assert "files" not in item
    assert len(ids) == 5
    listing = await client.get("/api/projects/import/configurations", headers=auth)
    assert listing.status_code == 200 and len(listing.json()) == 5
    assert "content_base64" not in listing.text and "Review carefully" not in listing.text
    selected = await client.get(
        "/api/projects/import/configurations?source=codex&category=mcpServers", headers=auth
    )
    assert len(selected.json()) == 1 and selected.json()[0]["category"] == "mcpServers"
    assert (await client.get("/api/projects", headers=auth)).json() == []
    assert server.paths.config.read_bytes() == before_config
    assert server.services.agent_repo.list_all() == before_agents


async def test_configuration_dedup_details_owner_source_and_restart(env):
    client, server, auth = env
    body = configuration_body()
    imported = (
        await client.post("/api/projects/import/configurations", headers=auth, json=body)
    ).json()["configuration"]
    repeated = await client.post(
        "/api/projects/import/configurations",
        headers=auth,
        json=configuration_body(source_path="/another/source/location"),
    )
    assert repeated.json() == {"configuration": imported, "action": "unchanged"}
    other_source = await client.post(
        "/api/projects/import/configurations",
        headers=auth,
        json=configuration_body(source="claude"),
    )
    assert other_source.json()["configuration"]["configuration_id"] != imported["configuration_id"]
    base = f"/api/projects/import/configurations/{imported['configuration_id']}"
    detail = await client.get(base, headers=auth)
    assert detail.json()["files"] == body["files"]
    alice = await create_user(client, auth, username="alice")
    assert (await client.get(base, headers=alice)).status_code == 404
    assert (await client.get("/api/projects/import/configurations", headers=alice)).json() == []
    own = await client.post("/api/projects/import/configurations", headers=alice, json=body)
    assert own.json()["action"] == "imported"
    assert own.json()["configuration"]["configuration_id"] != imported["configuration_id"]
    run_migrations(server.services.db)
    assert (await client.get(base, headers=auth)).json() == detail.json()
    assert detail.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path", ["../secret", "/absolute", "a/../b", "a//b", "./x", "C:/x", "a\\b"]
)
async def test_configuration_rejects_nonrelative_or_ambiguous_paths(env, path):
    client, _server, auth = env
    body = configuration_body(files=[{"relative_path": path, "content_base64": "eA=="}])
    response = await client.post("/api/projects/import/configurations", headers=auth, json=body)
    assert response.status_code == 422


@pytest.mark.parametrize("content", ["not base64", "eA==extra", "é", "eA=", "e A=="])
async def test_configuration_rejects_invalid_base64(env, content):
    client, _server, auth = env
    body = configuration_body(files=[{"relative_path": "SKILL.md", "content_base64": content}])
    response = await client.post("/api/projects/import/configurations", headers=auth, json=body)
    assert response.status_code == 422


async def test_configuration_limits_and_duplicate_paths_preserve_existing_data(env, monkeypatch):
    client, _server, auth = env
    monkeypatch.setattr(import_models, "MAX_CONFIGURATION_BYTES", 3)
    response = await client.post(
        "/api/projects/import/configurations", headers=auth, json=configuration_body()
    )
    assert response.status_code == 422
    file = {"relative_path": "same", "content_base64": "eA=="}
    duplicate = await client.post(
        "/api/projects/import/configurations",
        headers=auth,
        json=configuration_body(files=[file, file]),
    )
    assert duplicate.status_code == 422
    assert (await client.get("/api/projects/import/configurations", headers=auth)).json() == []


async def test_import_documentation_has_typed_schemas(env, monkeypatch):
    from dataclasses import replace

    import httpx

    from octop.api.app import build_app

    _client, server, auth = env
    monkeypatch.setattr(
        server,
        "services",
        replace(server.services, config=replace(server.services.config, enable_api_docs=True)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(server)), base_url="http://testserver"
    ) as client:
        assert (await client.get("/api/docs", headers=auth)).status_code == 200
        spec = (await client.get("/api/openapi.json", headers=auth)).json()
    for path, method in (
        ("/api/projects/import", "post"),
        ("/api/projects/import/configurations", "post"),
        ("/api/projects/import/configurations", "get"),
        ("/api/projects/import/configurations/{configuration_id}", "get"),
    ):
        operation = spec["paths"][path][method]
        assert operation["summary"] and operation["description"]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"]
