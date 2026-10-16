"""Independent project persistence, ownership, and resource binding contracts."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from octop.infra.connectors.service import ConnectorService
from octop.infra.skills.skill_package_store import SkillPackageStore
from tests.support.auth import create_user, resolve_user_id


def resource_fixture(server, owner):
    services = server.services
    assert services is not None
    store = SkillPackageStore(
        repo=services.skill_package_repo, root=server.paths.skill_packages_dir
    )
    package = store.create(name="Project skills", description="fixture", created_by="admin")
    for slug in ("review", "research"):
        store.write_skill(
            package.id,
            slug,
            [
                (
                    "SKILL.md",
                    f"---\nname: {slug}\ndescription: fixture\n---\nWork carefully".encode(),
                )
            ],
        )
    connectors = ConnectorService(
        repo=services.connector_repo,
        secret_repo=services.secret_repo,
        settings_repo=services.settings_repo,
        config=services.config,
    )
    connectors.put_custom_servers(
        owner,
        {
            "notes": {
                "transport": "streamable_http",
                "url": "https://private.invalid/mcp",
                "headers": {"Authorization": "Bearer fixture-private-secret"},
            },
            "disabled": {
                "transport": "streamable_http",
                "url": "https://disabled.invalid/mcp",
                "enabled": False,
            },
        },
    )
    return package.id


async def test_project_create_edit_read_and_list_do_not_create_experts_or_require_model(
    env, tmp_path
):
    client, server, auth = env
    before = server.services.agent_repo.list_all()
    directory = tmp_path / "project"
    directory.mkdir()
    body = {
        "name": " 独立项目 ",
        "description": "项目简介",
        "directory": str(directory),
        "instructions": "先阅读再修改",
    }
    created = await client.post(
        "/api/projects", headers={**auth, "Idempotency-Key": str(uuid4())}, json=body
    )
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["name"] == "独立项目" and item["revision"] == 1
    assert item["directory"] == str(directory.resolve())
    assert not {"agent_id", "expert_id", "agents", "experts"} & item.keys()
    base = f"/api/projects/{item['project_id']}"
    assert (await client.get(base, headers=auth)).json() == item
    assert (await client.get("/api/projects", headers=auth)).json() == [item]
    changed = await client.patch(
        base,
        headers=auth,
        json={**body, "name": "改名后的项目", "directory": None, "expected_revision": 1},
    )
    assert changed.status_code == 200, changed.text
    result = changed.json()
    assert result["revision"] == 2 and result["directory"] is None
    assert result["created_at"] == item["created_at"]
    assert (await client.get(base, headers=auth)).json() == result
    assert (await client.get("/api/projects", headers=auth)).json() == [result]
    assert changed.headers["cache-control"] == "no-store"
    assert server.services.agent_repo.list_all() == before
    # The new resource survives reopening its SQL repository, without an agent mapping.
    with server.services.db.connect() as conn:
        saved = conn.execute(
            "SELECT * FROM projects WHERE project_id=?", (item["project_id"],)
        ).fetchone()
    assert saved["instructions"] == body["instructions"]


async def test_project_can_link_multiple_skills_and_mcp_without_leaking_credentials(env):
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    package = resource_fixture(server, owner)
    catalog = await client.get("/api/projects/resources", headers=auth)
    assert catalog.status_code == 200, catalog.text
    assert len(catalog.json()["skills"]) == 2
    assert {m["server_name"] for m in catalog.json()["mcp_servers"]} == {"notes", "disabled"}
    assert "fixture-private-secret" not in catalog.text and "private.invalid" not in catalog.text
    fields = {
        "name": "能力配置",
        "skills": [{"package_id": package, "slug": s} for s in ("review", "research")],
        "mcp_servers": ["notes", "disabled"],
    }
    created = await client.post(
        "/api/projects", headers={**auth, "Idempotency-Key": str(uuid4())}, json=fields
    )
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["skills"] == fields["skills"] and item["mcp_servers"] == fields["mcp_servers"]
    base = f"/api/projects/{item['project_id']}"
    response = await client.patch(
        base,
        headers=auth,
        json={**fields, "skills": fields["skills"][:1], "mcp_servers": [], "expected_revision": 1},
    )
    assert response.status_code == 200 and response.json()["mcp_servers"] == []
    assert len((await client.get(base, headers=auth)).json()["skills"]) == 1


async def test_project_isolation_and_mcp_ownership(env):
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    resource_fixture(server, owner)
    alice = await create_user(client, auth, username="alice")
    item = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Private"},
        )
    ).json()
    base = f"/api/projects/{item['project_id']}"
    assert (await client.get("/api/projects", headers=alice)).json() == []
    assert (await client.get(base, headers=alice)).status_code == 404
    assert (
        await client.patch(base, headers=alice, json={"name": "Take over", "expected_revision": 1})
    ).status_code == 404
    assert (
        await client.post(
            "/api/projects",
            headers={**alice, "Idempotency-Key": str(uuid4())},
            json={"name": "Private"},
        )
    ).status_code == 201
    forbidden = await client.post(
        "/api/projects",
        headers={**alice, "Idempotency-Key": str(uuid4())},
        json={"name": "MCP", "mcp_servers": ["notes"]},
    )
    assert (
        forbidden.status_code == 422
        and forbidden.json()["error"]["code"] == "PROJECT_RESOURCE_INVALID"
    )
    assert (await client.get("/api/projects/resources", headers=alice)).json()["mcp_servers"] == []


async def test_revision_and_duplicate_name_conflicts_preserve_saved_project(env):
    client, _, auth = env
    item = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "First"},
        )
    ).json()
    base = f"/api/projects/{item['project_id']}"
    duplicate = await client.post(
        "/api/projects", headers={**auth, "Idempotency-Key": str(uuid4())}, json={"name": " First "}
    )
    assert duplicate.status_code == 409
    await client.post(
        "/api/projects", headers={**auth, "Idempotency-Key": str(uuid4())}, json={"name": "Second"}
    )
    duplicate_edit = await client.patch(
        base, headers=auth, json={"name": "Second", "expected_revision": 1}
    )
    assert duplicate_edit.status_code == 409
    assert (
        await client.patch(base, headers=auth, json={"name": "Updated", "expected_revision": 1})
    ).status_code == 200
    stale = await client.patch(
        base, headers=auth, json={"name": "Overwrite", "expected_revision": 1}
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "PROJECT_REVISION_CONFLICT"
    assert (await client.get(base, headers=auth)).json()["name"] == "Updated"


@pytest.mark.parametrize(
    "invalid",
    [
        {"name": " "},
        {"name": "Bad", "expert_id": "anything"},
        {"name": "Bad", "skills": [{"package_id": "missing", "slug": "x"}]},
        {"name": "Bad", "mcp_servers": ["unknown"]},
        {"name": "Bad", "mcp_servers": ["x", "x"]},
    ],
)
async def test_invalid_project_inputs_are_rejected(env, invalid):
    client, _, auth = env
    assert (
        await client.post(
            "/api/projects", headers={**auth, "Idempotency-Key": str(uuid4())}, json=invalid
        )
    ).status_code == 422
    assert (await client.get("/api/projects", headers=auth)).json() == []


async def test_missing_directory_and_restricted_directory_are_rejected(env, tmp_path):
    client, server, auth = env
    missing = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"name": "Missing", "directory": str(tmp_path / "missing")},
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "PROJECT_DIRECTORY_INVALID"
    owner = await resolve_user_id(client, auth, "admin")
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    server.services.user_policy_repo.set(owner, "workspace_root_dir", str(allowed))
    blocked = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"name": "Outside", "directory": str(outside)},
    )
    assert blocked.status_code == 422


async def test_projects_require_authentication(env):
    client, _, _ = env
    assert (await client.get("/api/projects")).status_code == 401


async def test_skillhub_references_persist_without_a_package_or_expert(env):
    client, server, auth = env
    before = server.services.agent_repo.list_all()
    assert server.services.skill_package_repo.list_all() == []
    body = {
        "name": "育儿项目",
        "skills": [
            {
                "source": "skillhub",
                "slug": "parenting-expert",
                "name": "育儿大师.Skill",
                "description": "育儿方法",
            },
            {
                "source": "skillhub",
                "slug": "parenting-methodology",
                "name": "育儿助手",
                "description": "",
            },
        ],
    }
    created = await client.post(
        "/api/projects", headers={**auth, "Idempotency-Key": str(uuid4())}, json=body
    )
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["skills"] == body["skills"]
    base = f"/api/projects/{item['project_id']}"
    assert (await client.get(base, headers=auth)).json()["skills"] == body["skills"]
    assert (await client.get("/api/projects", headers=auth)).json()[0]["skills"] == body["skills"]
    changed = await client.patch(
        base, headers=auth, json={**body, "expected_revision": 1, "skills": body["skills"][:1]}
    )
    assert changed.status_code == 200
    assert (await client.get(base, headers=auth)).json()["skills"] == body["skills"][:1]
    assert server.services.agent_repo.list_all() == before
    assert server.services.skill_package_repo.list_all() == []


@pytest.mark.parametrize(
    "skills",
    [
        [{"source": "skillhub", "slug": "../escape", "name": "Bad"}],
        [{"source": "skillhub", "slug": "missing-name"}],
        [{"source": "skillhub", "slug": "repeated", "name": "A"}] * 2,
    ],
)
async def test_invalid_or_repeated_skillhub_references_are_rejected(env, skills):
    client, _, auth = env
    result = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"name": "Invalid", "skills": skills},
    )
    assert result.status_code == 422


@pytest.mark.parametrize(
    "client_path",
    [
        "/Users/alice/My Projects/demo",
        "/home/alice/projects/客户端项目",
        r"C:\Users\alice\My Projects\demo",
        r"\\workstation\share\projects\demo",
    ],
)
async def test_client_directory_is_saved_verbatim_without_server_filesystem_checks(
    env, client_path
):
    # These are remote path strings, deliberately independent of the test host OS.
    client, _, auth = env
    created = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"name": "Client project", "client_directory": client_path},
    )
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["client_directory"] == client_path
    assert project["directory"] is None
    base = f"/api/projects/{project['project_id']}"
    assert (await client.get(base, headers=auth)).json()["client_directory"] == client_path
    assert (await client.get("/api/projects", headers=auth)).json()[0][
        "client_directory"
    ] == client_path


async def test_server_and_client_directories_can_be_changed_and_cleared_independently(
    env, tmp_path
):
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    server.services.user_policy_repo.set(owner, "workspace_root_dir", str(tmp_path))
    body = {
        "name": "Two directories",
        "directory": str(tmp_path),
        "client_directory": r"C:\Users\alice\project",
    }
    created = await client.post(
        "/api/projects", headers={**auth, "Idempotency-Key": str(uuid4())}, json=body
    )
    assert created.status_code == 201, created.text
    project = created.json()
    base = f"/api/projects/{project['project_id']}"
    result = await client.patch(
        base, headers=auth, json={**body, "expected_revision": 1, "client_directory": None}
    )
    assert result.status_code == 200
    assert result.json()["directory"] == str(tmp_path.resolve())
    assert result.json()["client_directory"] is None
    result = await client.patch(
        base, headers=auth, json={**body, "expected_revision": 2, "directory": None}
    )
    assert result.status_code == 200
    assert result.json()["directory"] is None
    assert result.json()["client_directory"] == body["client_directory"]
    invalid_server = await client.patch(
        base,
        headers=auth,
        json={**body, "expected_revision": 3, "directory": str(tmp_path / "missing")},
    )
    assert invalid_server.status_code == 422
    assert (await client.get(base, headers=auth)).json() == result.json()


@pytest.mark.parametrize(
    "client_path",
    [
        "",
        " ",
        "relative/project",
        "~/project",
        "C:relative",
        "/client\x00/project",
        "/client\n/project",
    ],
)
async def test_client_directory_rejects_relative_paths_and_control_characters(env, client_path):
    client, _, auth = env
    result = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"name": "Invalid path", "client_directory": client_path},
    )
    assert result.status_code == 422


async def test_patch_rename_preserves_directories_skills_mcp_and_instructions(env, tmp_path):
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    package = resource_fixture(server, owner)
    fields = {
        "name": "Before",
        "description": "Keep description",
        "directory": str(tmp_path),
        "client_directory": r"C:\Users\alice\project",
        "instructions": "Keep instructions",
        "skills": [{"package_id": package, "slug": "review"}],
        "mcp_servers": ["notes"],
    }
    created = await client.post(
        "/api/projects", headers={**auth, "Idempotency-Key": str(uuid4())}, json=fields
    )
    assert created.status_code == 201, created.text
    original = created.json()
    base = f"/api/projects/{original['project_id']}"
    changed = await client.patch(base, headers=auth, json={"name": "After", "expected_revision": 1})
    assert changed.status_code == 200, changed.text
    result = changed.json()
    for key in fields.keys() - {"name"}:
        assert result[key] == original[key]
    assert result["name"] == "After" and result["revision"] == 2
    cleared = await client.patch(
        base,
        headers=auth,
        json={
            "client_directory": None,
            "skills": [],
            "expected_revision": 2,
        },
    )
    assert cleared.status_code == 200
    assert cleared.json()["client_directory"] is None and cleared.json()["skills"] == []
    assert cleared.json()["directory"] == original["directory"]
    assert cleared.json()["mcp_servers"] == original["mcp_servers"]
    stale = await client.patch(base, headers=auth, json={"name": "Stale", "expected_revision": 2})
    assert stale.status_code == 409
    assert (await client.get(base, headers=auth)).json() == cleared.json()
    # Full replacement must be explicit rather than silently resetting omitted fields.
    assert (
        await client.put(base, headers=auth, json={"name": "Oops", "expected_revision": 3})
    ).status_code == 422


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"name": None},
        {"description": None},
        {"instructions": None},
        {"skills": None},
        {"mcp_servers": None},
        {"client_directory": "relative"},
        {"skills": [{"source": "skillhub", "slug": "x", "name": "X"}] * 2},
    ],
)
async def test_invalid_patch_does_not_change_project(env, changes):
    client, _, auth = env
    original = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Saved"},
        )
    ).json()
    base = f"/api/projects/{original['project_id']}"
    response = await client.patch(base, headers=auth, json={"expected_revision": 1, **changes})
    assert response.status_code == 422, response.text
    assert (await client.get(base, headers=auth)).json() == original


async def test_project_creation_is_durable_idempotent_and_owner_scoped(env):
    client, server, auth = env
    key = str(uuid4())
    headers = {**auth, "Idempotency-Key": key}
    fields = {"name": "First", "instructions": "Keep the original receipt"}
    responses = await asyncio.gather(
        *[client.post("/api/projects", headers=headers, json=fields) for _ in range(3)]
    )
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    original = responses[0].json()
    assert all(r.json() == original for r in responses)
    assert len((await client.get("/api/projects", headers=auth)).json()) == 1
    await client.patch(
        f"/api/projects/{original['project_id']}",
        headers=auth,
        json={"name": "Renamed", "expected_revision": 1},
    )
    replay = await client.post("/api/projects", headers=headers, json=fields)
    assert replay.status_code == 201 and replay.json() == original
    conflict = await client.post("/api/projects", headers=headers, json={"name": "Different"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "PROJECT_REQUEST_CONFLICT"
    alice = await create_user(client, auth, username="alice")
    another = await client.post(
        "/api/projects", headers={**alice, "Idempotency-Key": key}, json=fields
    )
    assert another.status_code == 201 and another.json()["project_id"] != original["project_id"]
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_create_requests").fetchone()[0] == 2


async def test_failed_creation_does_not_consume_key_and_key_is_required(env, tmp_path):
    client, _, auth = env
    assert (
        await client.post("/api/projects", headers=auth, json={"name": "Missing key"})
    ).status_code == 422
    headers = {**auth, "Idempotency-Key": str(uuid4())}
    directory = tmp_path / "later"
    fields = {"name": "Retry", "directory": str(directory)}
    assert (await client.post("/api/projects", headers=headers, json=fields)).status_code == 422
    directory.mkdir()
    assert (await client.post("/api/projects", headers=headers, json=fields)).status_code == 201
    directory.rmdir()
    # A lost-response retry is a receipt read, not a second resource validation or creation.
    assert (await client.post("/api/projects", headers=headers, json=fields)).status_code == 201


async def test_project_sync_coalesces_and_freezes_delta_pages(env):
    client, server, auth = env
    base = (await client.get("/api/projects/sync", headers=auth)).json()
    assert base["mode"] == "snapshot" and base["changes"] == []
    created = []
    for name in ("Sync A", "Sync B", "Sync C"):
        key = str(uuid4())
        response = await client.post(
            "/api/projects", headers={**auth, "Idempotency-Key": key}, json={"name": name}
        )
        assert response.status_code == 201
        created.append(response.json())
        assert (
            await client.post(
                "/api/projects", headers={**auth, "Idempotency-Key": key}, json={"name": name}
            )
        ).json() == response.json()
    params = {"sync_cursor": base["next_sync_cursor"], "limit": 1}
    first = (await client.get("/api/projects/sync", headers=auth, params=params)).json()
    assert len(first["changes"]) == 1 and first["next_sync_cursor"] is None
    assert first["next_page_cursor"]
    last = max(created, key=lambda row: row["project_id"])
    for revision in (1, 2):
        result = await client.patch(
            f"/api/projects/{last['project_id']}",
            headers=auth,
            json={"expected_revision": revision, "description": f"Version {revision + 1}"},
        )
        assert result.status_code == 200
    changes = list(first["changes"])
    page = first
    while page["next_page_cursor"]:
        response = await client.get(
            "/api/projects/sync",
            headers=auth,
            params={**params, "page_cursor": page["next_page_cursor"]},
        )
        assert response.status_code == 200, response.text
        page = response.json()
        changes.extend(page["changes"])
    assert len(changes) == 3
    assert all(change["data"]["revision"] == 1 for change in changes)
    delta = (
        await client.get(
            "/api/projects/sync", headers=auth, params={"sync_cursor": page["next_sync_cursor"]}
        )
    ).json()
    assert len(delta["changes"]) == 1
    assert delta["changes"][0]["data"]["revision"] == 3
    assert delta["changes"][0]["data"]["description"] == "Version 3"
    empty = (
        await client.get(
            "/api/projects/sync", headers=auth, params={"sync_cursor": delta["next_sync_cursor"]}
        )
    ).json()
    assert empty["changes"] == []
    # Journal and business writes share the same transaction; retries made no extra entries.
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_sync_changes").fetchone()[0] == 5
