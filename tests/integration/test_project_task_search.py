"""Global task search uses the complete owned catalogue and opaque list cursors."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from octop.infra.db.repos.project_tasks import ProjectTaskRow
from tests.support.auth import create_user, resolve_user_id


@pytest.fixture
async def search_env(env):
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    return client, server, auth, owner


async def project(client, auth, name):
    response = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"name": name, "instructions": "private-project-guide"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def seed(server, owner, identifier, title, *, project_id=None, updated_at=1000, **fields):
    row = ProjectTaskRow(
        task_id=identifier,
        owner_id=owner,
        project_id=project_id,
        title=title,
        request_id=f"request-{identifier}",
        active_turn_id=None,
        created_at=1000,
        updated_at=updated_at,
        status="completed",
        **fields,
    )
    with server.services.db.transaction() as conn:
        server.services.project_task_repo.insert(conn, row)
    return row


async def search(client, auth, q="", **params):
    response = await client.get(
        "/api/project-tasks",
        headers={**auth, "Accept": "application/json", "Cache-Control": "no-store"},
        params={"scope": "all", "q": q, "archived": False, "limit": 50, **params},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";", 1)[0] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["scope"] == "all" and body["q"] == q.strip()
    if "page_cursor" in params:
        assert body["sync_cursor"] is None
    else:
        assert body["sync_cursor"]
    identifiers = [row["task_id"] for row in body["items"]]
    assert all(identifiers) and len(identifiers) == len(set(identifiers))
    for row in body["items"]:
        assert row["list_revision"] > 0
        assert {"project_id", "title", "status", "active_turn_id", "archived"} <= row.keys()
        assert {"approval_policy", "configuration_revision", "web_search_enabled"} <= row.keys()
        assert not {"turns", "prompt", "reply", "configuration_json", "instructions"} & row.keys()
        for field in ("created_at", "updated_at"):
            assert datetime.fromisoformat(row[field].replace("Z", "+00:00")).tzinfo is not None
    return body


def identifiers(page):
    return [row["task_id"] for row in page["items"]]


async def test_global_search_matches_title_and_current_project_with_literal_unicode_words(
    search_env,
):
    client, server, auth, owner = search_env
    catalogue = await project(client, auth, "产品调研 Café ＧｉｔＨｕｂ")
    seed(
        server,
        owner,
        "task_project_match",
        "连接器 检查 100% under_score bang! quote'\"",
        project_id=catalogue["project_id"],
    )
    seed(
        server,
        owner,
        "task_standalone_match",
        "产品调研 连接器 Café GitHub 100% under_score bang! quote'\"",
    )
    seed(server, owner, "task_partial_match", "产品调研 1000 underXscore bang quote")
    seed(server, owner, "task_other_partial", "连接器")
    expected = {"task_project_match", "task_standalone_match"}
    for query in (
        "\u2003产品调研\t  连接器\u3000",
        "连接器 产品调研",
        "cafe GITHUB",
        "ｃａｆｅ github",
        "Cafe\u0301 github",
        "%",
        "_",
        "!",
        "'",
        '"',
    ):
        page = await search(client, auth, query)
        assert set(identifiers(page)) == expected, query
        assert page["next_page_cursor"] is None
    for query in ("不存在的词", "连接器 不存在", "private-project-guide", "' OR 1=1 --"):
        page = await search(client, auth, query)
        assert page["items"] == [] and page["next_page_cursor"] is None
    empty = await search(client, auth, " \t\u2003 ")
    assert set(identifiers(empty)) == expected | {"task_partial_match", "task_other_partial"}
    omitted = await client.get(
        "/api/project-tasks", headers=auth, params={"scope": "all", "archived": False}
    )
    assert omitted.status_code == 200, omitted.text
    assert omitted.json()["scope"] == "all" and omitted.json()["q"] == ""


async def test_global_search_filters_before_paging_and_preserves_order_and_visibility(search_env):
    client, server, auth, owner = search_env
    first = await project(client, auth, "Unopened catalogue")
    second = await project(client, auth, "Another catalogue")
    projects = [first["project_id"], second["project_id"], None]
    expected = []
    for index in range(125):
        expected.append(
            seed(
                server,
                owner,
                f"task_match_{index:03d}",
                f"Batch Match {index}",
                project_id=projects[index % len(projects)],
                updated_at=2000 + index // 4,
                pinned=index % 17 == 0,
            )
        )
    for index in range(60):
        seed(
            server,
            owner,
            f"task_recent_{index:03d}",
            f"Unrelated recent task {index}",
            project_id=first["project_id"],
            updated_at=5000 + index,
        )
    seed(
        server,
        owner,
        "task_old_unique",
        "OldUnique",
        project_id=first["project_id"],
        updated_at=1,
    )
    other_auth = await create_user(client, auth, username="search-other")
    other_owner = await resolve_user_id(client, auth, "search-other")
    seed(server, other_owner, "task_other_owner", "Batch Match", updated_at=9000)
    seed(server, owner, "task_archived", "Batch Match", archived=True, updated_at=9000)
    seed(server, owner, "task_internal", "Batch Match", internal_kind="resident", updated_at=9000)

    # Search has never opened either project's directory, including its older pages.
    assert identifiers(await search(client, auth, "OldUnique")) == ["task_old_unique"]
    detail = await client.get("/api/project-tasks/task_old_unique", headers=auth)
    assert detail.status_code == 200, detail.text
    assert detail.json()["project_id"] == first["project_id"]
    directory = await client.get(f"/api/projects/{first['project_id']}/tasks", headers=auth)
    assert directory.status_code == 200, directory.text
    assert "task_old_unique" not in identifiers(directory.json())
    assert directory.json()["next_page_cursor"]

    found, page_sizes, seen_cursors = [], [], set()
    cursor = None
    while True:
        page = await search(
            client, auth, "  batch MATCH  ", **({"page_cursor": cursor} if cursor else {})
        )
        found.extend(identifiers(page))
        page_sizes.append(len(page["items"]))
        cursor = page["next_page_cursor"]
        if cursor is None:
            break
        assert cursor not in seen_cursors
        seen_cursors.add(cursor)
    expected.sort(key=lambda row: (row.pinned, row.updated_at, row.task_id), reverse=True)
    assert found == [row.task_id for row in expected]
    assert page_sizes == [50, 50, 25]
    assert identifiers(await search(client, auth, "Batch Match", archived=True)) == [
        "task_archived"
    ]
    assert identifiers(await search(client, other_auth, "Batch Match")) == ["task_other_owner"]


async def test_global_search_cursors_bind_owner_scope_query_archive_and_reject_invalid_input(
    search_env,
):
    client, server, auth, owner = search_env
    for index in range(3):
        seed(server, owner, f"task_cursor_{index}", "Alpha")
    first = await search(client, auth, "Alpha", limit=1)
    cursor = first["next_page_cursor"]
    assert cursor
    other = await create_user(client, auth, username="cursor-other")
    base = {"scope": "all", "q": "Alpha", "archived": False, "limit": 1, "page_cursor": cursor}
    for headers, changes in (
        (auth, {"q": "Beta"}),
        (auth, {"archived": True}),
        (auth, {"scope": "standalone"}),
        (other, {}),
        (auth, {"page_cursor": ""}),
        (auth, {"page_cursor": "bad.游标"}),
        (auth, {"page_cursor": cursor + "x"}),
        (auth, {"page_cursor": first["sync_cursor"]}),
    ):
        response = await client.get(
            "/api/project-tasks", headers=headers, params={**base, **changes}
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "PROJECT_SYNC_RESET"
    response = await client.get(
        "/api/project-tasks/sync",
        headers=auth,
        params={"scope": "all", "sync_cursor": first["sync_cursor"]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PROJECT_SYNC_RESET"
    assert identifiers(await search(client, auth, "Alpha", limit=1)) == identifiers(first)
    for changes in ({"q": "x" * 257}, {"page_cursor": "游" * 4097}, {"limit": 0}, {"limit": 101}):
        response = await client.get("/api/project-tasks", headers=auth, params={**base, **changes})
        assert response.status_code == 422, response.text


async def test_global_search_requery_uses_project_and_task_renames_status_and_deletions(search_env):
    client, server, auth, owner = search_env
    catalogue = await project(client, auth, "旧项目")
    task = seed(server, owner, "task_changing", "旧任务", project_id=catalogue["project_id"])
    assert identifiers(await search(client, auth, "旧项目 旧任务")) == [task.task_id]
    response = await client.patch(
        f"/api/projects/{catalogue['project_id']}",
        headers=auth,
        json={"expected_revision": 1, "name": "新项目"},
    )
    assert response.status_code == 200, response.text
    assert identifiers(await search(client, auth, "旧项目")) == []
    assert identifiers(await search(client, auth, "新项目 旧任务")) == [task.task_id]
    response = await client.patch(
        f"/api/project-tasks/{task.task_id}",
        headers=auth,
        json={"expected_revision": task.list_revision, "title": "新任务"},
    )
    assert response.status_code == 200, response.text
    renamed = response.json()
    assert identifiers(await search(client, auth, "旧任务")) == []
    assert (await search(client, auth, "新项目 新任务"))["items"][0]["title"] == "新任务"

    with server.services.db.transaction() as conn:
        server.services.project_task_repo.activate(conn, task.task_id, "search-turn", 6000)
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
            "configuration_json,model_ref,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'{}','test/model',6000,6000)",
            ("search-turn", task.task_id, "search-turn-request", "Private prompt", "completed"),
        )
        server.services.project_task_repo.start(conn, task.task_id, "search-turn", 6001)
    running = (await search(client, auth, "新项目 新任务"))["items"][0]
    assert running["status"] == "running" and running["active_turn_id"] == "search-turn"
    assert running["list_revision"] > renamed["list_revision"]
    with server.services.db.transaction() as conn:
        server.services.project_task_repo.finish(conn, task.task_id, "search-turn", 6002)
    done = (await search(client, auth, "新项目 新任务"))["items"][0]
    assert done["status"] == "completed" and done["active_turn_id"] is None
    assert done["list_revision"] > running["list_revision"]
    response = await client.delete(f"/api/project-tasks/{task.task_id}", headers=auth)
    assert response.status_code == 204, response.text
    assert identifiers(await search(client, auth, "新任务")) == []
    assert (await client.get(f"/api/project-tasks/{task.task_id}", headers=auth)).status_code == 404

    seed(server, owner, "task_deleted_project", "项目任务", project_id=catalogue["project_id"])
    assert identifiers(await search(client, auth, "新项目")) == ["task_deleted_project"]
    response = await client.delete(f"/api/projects/{catalogue['project_id']}", headers=auth)
    assert response.status_code == 204, response.text
    assert identifiers(await search(client, auth, "新项目")) == []
