"""Browse published task snapshots across projects without reading file bytes."""

from __future__ import annotations

import hashlib
import sqlite3
from uuid import uuid4

import pytest

from octop.infra.db.repos.project_task_artifacts import ProjectTaskArtifactRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from tests.support.auth import create_user, resolve_user_id

BASE = "/api/project-tasks/artifacts"


def seed_task(services, owner, task_id, *, project_id=None, archived=False, updated_at=1000):
    with services.db.transaction() as conn:
        services.project_task_repo.insert(
            conn,
            ProjectTaskRow(
                task_id,
                owner,
                project_id,
                f"Title {task_id}",
                task_id,
                None,
                1000,
                updated_at,
                status="completed",
            ),
        )
        conn.execute(
            "UPDATE project_tasks SET archived=? WHERE task_id=?", (int(archived), task_id)
        )
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
            "configuration_json,model_ref,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"turn_{task_id}",
                task_id,
                task_id,
                "private prompt",
                "completed",
                "{}",
                "",
                1000,
                1000,
            ),
        )


def seed_artifact(
    services,
    task_id,
    artifact_id,
    *,
    name="report.pdf",
    created_at=2000,
    media_type="application/octet-stream",
    content=b"immutable original",
    business_category="uncategorized",
):
    with services.db.transaction() as conn:
        services.project_task_artifact_repo.insert(
            conn,
            ProjectTaskArtifactRow(
                artifact_id,
                task_id,
                f"turn_{task_id}",
                artifact_id,
                "request-hash",
                name,
                media_type,
                len(content),
                hashlib.sha256(content).hexdigest(),
                f"output/{name}",
                created_at,
                business_category=business_category,
            ),
            content,
        )


async def create_project(client, auth, name):
    response = await client.post(
        "/api/projects", headers={**auth, "Idempotency-Key": str(uuid4())}, json={"name": name}
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def library_env(env):
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    await server.project_task_worker.close()
    return client, server.services, auth, owner


async def test_library_orders_publications_across_tasks_and_pages_equal_timestamps(library_env):
    client, services, auth, owner = library_env
    project = await create_project(client, auth, "Library project")
    seed_task(services, owner, "project_task", project_id=project["project_id"], updated_at=9000)
    seed_task(services, owner, "standalone_task", updated_at=1000)
    seed_artifact(services, "project_task", "artifact_old", created_at=1000)
    seed_artifact(services, "standalone_task", "artifact_b", created_at=2000)
    seed_artifact(services, "project_task", "artifact_a", created_at=2000)
    seed_artifact(services, "standalone_task", "artifact_z", created_at=3000)

    response = await client.get(BASE, headers=auth, params={"limit": 2})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    first = response.json()
    assert [item["artifact_id"] for item in first["items"]] == ["artifact_z", "artifact_b"]
    assert first["items"][0]["project_id"] is None
    assert first["items"][0]["project_name"] is None
    assert first["items"][0]["project_archived"] is None
    assert first["next_page_cursor"]

    # A new publication appears on refresh, without shifting the continuation page.
    seed_artifact(services, "project_task", "artifact_new", created_at=4000)
    next_response = await client.get(
        BASE, headers=auth, params={"limit": 100, "page_cursor": first["next_page_cursor"]}
    )
    assert next_response.status_code == 200, next_response.text
    second = next_response.json()
    assert [item["artifact_id"] for item in second["items"]] == ["artifact_a", "artifact_old"]
    assert second["next_page_cursor"] is None
    assert second["items"][0]["project_name"] == "Library project"
    assert second["items"][0]["task_title"] == "Title project_task"
    assert second["items"][0]["turn_status"] == "completed"
    assert (await client.get(BASE, headers=auth)).json()["items"][0][
        "artifact_id"
    ] == "artifact_new"


async def test_library_metadata_does_not_load_content_or_conversation(library_env, monkeypatch):
    client, services, auth, owner = library_env
    seed_task(services, owner, "metadata_task")
    seed_artifact(services, "metadata_task", "artifact_metadata")

    def forbid_history(*args, **kwargs):
        pytest.fail("The library must not load task conversations")

    monkeypatch.setattr(services.project_task_turn_repo, "page", forbid_history)
    monkeypatch.setattr(services.project_task_turn_repo, "list_in_transaction", forbid_history)

    def metadata_only(action, table, column, *_):
        if (
            action == sqlite3.SQLITE_READ
            and table == "project_task_artifacts"
            and column == "content"
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with services.db.connect() as conn:
        conn.set_authorizer(metadata_only)
    try:
        listed = await client.get(BASE, headers=auth)
        detail = await client.get(f"{BASE}/artifact_metadata", headers=auth)
    finally:
        with services.db.connect() as conn:
            conn.set_authorizer(None)
    assert listed.status_code == detail.status_code == 200
    assert detail.headers["cache-control"] == "private, no-store"
    assert detail.json() == listed.json()["items"][0]
    assert not {"content", "prompt", "reply", "configuration_json"} & detail.json().keys()


async def test_library_filters_before_paging_and_searches_literal_names(library_env):
    client, services, auth, owner = library_env
    project = await create_project(client, auth, "Archived library project")
    seed_task(services, owner, "active_task")
    seed_task(services, owner, "archived_task", archived=True)
    seed_task(services, owner, "project_task", project_id=project["project_id"])
    seed_artifact(services, "active_task", "literal_a", name="REPORT%_!draft.PDF", created_at=1000)
    seed_artifact(services, "active_task", "literal_b", name="report%_!draft.PDF", created_at=2000)
    seed_artifact(
        services, "active_task", "not_literal", name="reportXX!draft.pdf", created_at=3000
    )
    seed_artifact(services, "active_task", "image", name="picture.png", created_at=4000)
    seed_artifact(services, "archived_task", "archived", name="history.md", created_at=5000)
    seed_artifact(services, "project_task", "project", name="slides.pptx", created_at=6000)
    response = await client.patch(
        f"/api/projects/{project['project_id']}/archive",
        headers=auth,
        json={"expected_revision": project["revision"], "archived": True},
    )
    assert response.status_code == 200, response.text

    filters = {"kind": "document", "q": "  report%_!DRAFT  ", "archived": "active", "limit": 1}
    matched = await client.get(BASE, headers=auth, params=filters)
    assert matched.status_code == 200, matched.text
    first = matched.json()
    assert [item["artifact_id"] for item in first["items"]] == ["literal_b"]
    assert first["items"][0]["file_kind"] == "document"
    continuation = {**filters, "q": "report%_!DRAFT", "page_cursor": first["next_page_cursor"]}
    second = await client.get(BASE, headers=auth, params=continuation)
    assert second.status_code == 200, second.text
    assert [item["artifact_id"] for item in second.json()["items"]] == ["literal_a"]
    assert second.json()["next_page_cursor"] is None
    for changed in (
        {"q": "different"},
        {"kind": "all"},
        {"business_category": "report"},
        {"archived": "all"},
        {"scope": "standalone"},
        {"task_id": "active_task"},
        {"scope": "project", "project_id": project["project_id"]},
    ):
        rejected = await client.get(BASE, headers=auth, params={**continuation, **changed})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["error"]["code"] == "PROJECT_SYNC_RESET"
    archived = (await client.get(BASE, headers=auth, params={"archived": "archived"})).json()[
        "items"
    ]
    assert [item["artifact_id"] for item in archived] == ["project", "archived"]
    assert archived[0]["project_archived"] and not archived[0]["task_archived"]
    assert archived[1]["task_archived"] and archived[1]["project_archived"] is None
    scoped = await client.get(
        BASE, headers=auth, params={"scope": "project", "project_id": project["project_id"]}
    )
    assert [item["artifact_id"] for item in scoped.json()["items"]] == ["project"]
    mismatch = await client.get(
        BASE, headers=auth, params={"scope": "standalone", "task_id": "project_task"}
    )
    assert mismatch.json() == {"items": [], "next_page_cursor": None}


async def test_library_kind_filters_use_extension_then_mime_fallback(library_env):
    client, services, auth, owner = library_env
    seed_task(services, owner, "kinds_task")
    for artifact_id, name, media_type in (
        ("mime_document", "report.unknown", "application/pdf"),
        ("extension_text", "report.md", "application/pdf"),
        ("unknown", "README", "application/octet-stream"),
    ):
        seed_artifact(services, "kinds_task", artifact_id, name=name, media_type=media_type)
    listed = (await client.get(BASE, headers=auth)).json()["items"]
    assert {item["artifact_id"]: item["file_kind"] for item in listed} == {
        "mime_document": "document",
        "extension_text": "text",
        "unknown": "other",
    }
    for kind, expected in (
        ("document", "mime_document"),
        ("text", "extension_text"),
        ("other", "unknown"),
    ):
        filtered = await client.get(BASE, headers=auth, params={"kind": kind, "limit": 1})
        assert filtered.status_code == 200, filtered.text
        assert [item["artifact_id"] for item in filtered.json()["items"]] == [expected]
        assert filtered.json()["next_page_cursor"] is None


async def test_business_categories_are_explicit_and_independent_of_file_kind(library_env):
    client, services, auth, owner = library_env
    seed_task(services, owner, "categories_task")
    for artifact_id, name, category in (
        ("report_markdown", "report.md", "report"),
        ("data_markdown", "data.md", "data"),
        ("workflow_markdown", "workflow.md", "workflow"),
        ("report_csv", "report.csv", "report"),
        ("unclassified_pdf", "report.pdf", "uncategorized"),
    ):
        seed_artifact(
            services, "categories_task", artifact_id, name=name, business_category=category
        )
    listed = await client.get(BASE, headers=auth)
    assert listed.status_code == 200, listed.text
    by_id = {item["artifact_id"]: item for item in listed.json()["items"]}
    assert set(by_id) == {
        "report_markdown",
        "data_markdown",
        "workflow_markdown",
        "report_csv",
        "unclassified_pdf",
    }
    assert {
        by_id[name]["file_kind"]
        for name in ("report_markdown", "data_markdown", "workflow_markdown")
    } == {"text"}
    assert by_id["report_csv"]["file_kind"] == "spreadsheet"
    assert by_id["unclassified_pdf"]["business_category"] == "uncategorized"
    assert (await client.get(BASE, headers=auth, params={"business_category": "all"})).json() == (
        listed.json()
    )
    for category, expected in (
        ("report", {"report_markdown", "report_csv"}),
        ("data", {"data_markdown"}),
        ("workflow", {"workflow_markdown"}),
        ("uncategorized", {"unclassified_pdf"}),
    ):
        filtered = await client.get(BASE, headers=auth, params={"business_category": category})
        assert filtered.status_code == 200, filtered.text
        assert {item["artifact_id"] for item in filtered.json()["items"]} == expected
        for artifact_id in expected:
            detail = await client.get(f"{BASE}/{artifact_id}", headers=auth)
            assert detail.json() == by_id[artifact_id]
            assert detail.json()["business_category"] == category


async def test_business_category_intersects_search_source_and_state_before_paging(library_env):
    client, services, auth, owner = library_env
    project = await create_project(client, auth, "Categorized library")
    other_project = await create_project(client, auth, "Other source")
    seed_task(services, owner, "category_project", project_id=project["project_id"])
    seed_task(services, owner, "category_other", project_id=other_project["project_id"])
    seed_task(services, owner, "category_standalone")
    seed_task(services, owner, "category_archived", project_id=project["project_id"], archived=True)
    for task_id, artifact_id, name, category, created_at in (
        ("category_project", "matching_old", "quarter-old.md", "report", 1000),
        ("category_project", "matching_new", "QUARTER-new.md", "report", 2000),
        ("category_project", "wrong_category", "quarter-data.md", "data", 3000),
        ("category_project", "wrong_name", "annual.md", "report", 4000),
        ("category_project", "wrong_format", "quarter.csv", "report", 5000),
        ("category_other", "wrong_project", "quarter.md", "report", 6000),
        ("category_standalone", "wrong_scope", "quarter.md", "report", 7000),
        ("category_archived", "wrong_state", "quarter.md", "report", 8000),
    ):
        seed_artifact(
            services,
            task_id,
            artifact_id,
            name=name,
            business_category=category,
            created_at=created_at,
        )
    filters = {
        "business_category": "report",
        "q": "quarter",
        "kind": "text",
        "scope": "project",
        "project_id": project["project_id"],
        "archived": "active",
        "limit": 1,
    }
    first = await client.get(BASE, headers=auth, params=filters)
    assert first.status_code == 200, first.text
    assert [item["artifact_id"] for item in first.json()["items"]] == ["matching_new"]
    cursor = first.json()["next_page_cursor"]
    assert cursor
    second = await client.get(BASE, headers=auth, params={**filters, "page_cursor": cursor})
    assert second.status_code == 200, second.text
    assert [item["artifact_id"] for item in second.json()["items"]] == ["matching_old"]
    assert second.json()["next_page_cursor"] is None
    for category in ("data", "all", "uncategorized"):
        rejected = await client.get(
            BASE,
            headers=auth,
            params={**filters, "business_category": category, "page_cursor": cursor},
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["error"]["code"] == "PROJECT_SYNC_RESET"
    for source in (
        {"task_id": "category_project"},
        {"scope": "project", "project_id": project["project_id"]},
    ):
        result = await client.get(
            BASE,
            headers=auth,
            params={"business_category": "data", "q": "quarter", **source},
        )
        assert [item["artifact_id"] for item in result.json()["items"]] == ["wrong_category"]


async def test_library_owner_isolation_and_untrusted_cursors(library_env):
    client, services, auth, owner = library_env
    alice = await create_user(client, auth, username="library-alice")
    alice_owner = await resolve_user_id(client, auth, "library-alice")
    alice_project = await create_project(client, alice, "Alice private project")
    seed_task(services, owner, "admin_task")
    seed_task(services, alice_owner, "alice_task", project_id=alice_project["project_id"])
    seed_artifact(services, "admin_task", "admin_a", business_category="report")
    seed_artifact(services, "admin_task", "admin_b", business_category="report")
    seed_artifact(services, "alice_task", "alice_a", business_category="report")
    assert (await client.get(BASE)).status_code == 401
    assert (await client.get(f"{BASE}/admin_a")).status_code == 401
    assert [
        item["artifact_id"] for item in (await client.get(BASE, headers=auth)).json()["items"]
    ] == ["admin_b", "admin_a"]
    assert [
        item["artifact_id"] for item in (await client.get(BASE, headers=alice)).json()["items"]
    ] == ["alice_a"]
    categorized = await client.get(BASE, headers=auth, params={"business_category": "report"})
    assert [item["artifact_id"] for item in categorized.json()["items"]] == ["admin_b", "admin_a"]
    alice_categorized = await client.get(
        BASE, headers=alice, params={"business_category": "report"}
    )
    assert [item["artifact_id"] for item in alice_categorized.json()["items"]] == ["alice_a"]
    for params in (
        {"task_id": "alice_task"},
        {"task_id": "missing"},
        {"scope": "project", "project_id": alice_project["project_id"]},
        {"scope": "project", "project_id": "missing"},
    ):
        assert (await client.get(BASE, headers=auth, params=params)).status_code == 404
    for artifact_id in ("alice_a", "missing"):
        assert (await client.get(f"{BASE}/{artifact_id}", headers=auth)).status_code == 404
    first = (await client.get(BASE, headers=auth, params={"limit": 1})).json()
    for headers, cursor in ((alice, first["next_page_cursor"]), (auth, "tampered-cursor")):
        rejected = await client.get(BASE, headers=headers, params={"page_cursor": cursor})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["error"]["code"] == "PROJECT_SYNC_RESET"


@pytest.mark.parametrize(
    "params",
    [
        {"scope": "unknown"},
        {"scope": "project"},
        {"project_id": "anything"},
        {"scope": "standalone", "project_id": "anything"},
        {"kind": "unknown"},
        {"business_category": "document"},
        {"business_category": "reports"},
        {"business_category": ""},
        {"archived": "unknown"},
        {"limit": 0},
        {"limit": 101},
        {"q": "x" * 201},
        {"q": "\u0000"},
        {"page_cursor": ""},
        {"page_cursor": "x" * 4097},
    ],
)
async def test_library_rejects_invalid_filters(library_env, params):
    client, _, auth, _ = library_env
    assert (await client.get(BASE, headers=auth, params=params)).status_code == 422


async def test_archived_snapshot_download_and_task_deletion(library_env):
    client, services, auth, owner = library_env
    seed_task(services, owner, "durable_task", archived=True)
    seed_artifact(
        services,
        "durable_task",
        "durable_artifact",
        name="归档文件.pdf",
        content=b"original\x00\xff",
    )
    detail = await client.get(f"{BASE}/durable_artifact", headers=auth)
    assert detail.status_code == 200, detail.text
    metadata = detail.json()
    assert metadata["task_archived"]
    downloaded = await client.get(metadata["download_url"], headers=auth)
    assert downloaded.status_code == 200 and downloaded.content == b"original\x00\xff"
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert (await client.delete("/api/project-tasks/durable_task", headers=auth)).status_code == 204
    assert (await client.get(BASE, headers=auth)).json() == {"items": [], "next_page_cursor": None}
    assert (await client.get(f"{BASE}/durable_artifact", headers=auth)).status_code == 404
    assert (await client.get(metadata["download_url"], headers=auth)).status_code == 404


async def test_library_routes_have_readable_openapi_models(env):
    client, _, _ = env
    schema = client._octop_app.openapi()
    for path in (BASE, f"{BASE}/{{artifact_id}}"):
        route = schema["paths"][path]["get"]
        assert route["summary"] and route["description"]
        assert "$ref" in route["responses"]["200"]["content"]["application/json"]["schema"]
    item = schema["components"]["schemas"]["TaskArtifactLibraryItem"]
    assert {
        "artifact_id",
        "created_at",
        "file_kind",
        "business_category",
        "task_title",
        "project_name",
    } <= item["properties"].keys()
    assert "business_category" in item["required"]
    assert set(item["properties"]["business_category"]["enum"]) == {
        "report",
        "data",
        "workflow",
        "uncategorized",
    }
    parameter = next(
        item
        for item in schema["paths"][BASE]["get"]["parameters"]
        if item["name"] == "business_category"
    )
    assert parameter["description"]
    assert parameter["schema"]["default"] == "all"
    assert set(parameter["schema"]["enum"]) == {
        "all",
        "report",
        "data",
        "workflow",
        "uncategorized",
    }
    assert "content" not in item["properties"]


@pytest.mark.parametrize(
    "pause_requested,expected", [(False, "waiting_approval"), (True, "paused")]
)
async def test_library_reports_public_turn_status(library_env, pause_requested, expected):
    client, services, auth, owner = library_env
    seed_task(services, owner, "waiting_task")
    seed_artifact(services, "waiting_task", "waiting_artifact")
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET status='running',worker_id='',checkpoint_id='checkpoint',"
            "wait_kind='approval',pause_requested=? WHERE task_id='waiting_task'",
            (int(pause_requested),),
        )
    response = await client.get(f"{BASE}/waiting_artifact", headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["turn_status"] == expected
