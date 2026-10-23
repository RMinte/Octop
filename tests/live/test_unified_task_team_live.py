"""Opt-in real-provider acceptance of project-scoped, shared expert teams."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest
from tests.live.unified_task_support import LiveTaskEnvironment, await_task, live_options
from tests.support.auth import auth_header

from octop.infra.projects.expert_profiles import _workspace

pytestmark = pytest.mark.live


def _require(response: Any, status: int) -> dict[str, Any]:
    body = response.json()
    assert response.status_code == status, (response.status_code, body.get("error", {}).get("code"))
    return body


def _seed_team(env: LiveTaskEnvironment, shared_owner: int) -> tuple[dict[str, Any], str]:
    services = env.server.services
    original = {}
    private_marker = "private-owner-only-" + uuid4().hex
    for expert_id, name, owner, filename in (
        ("live-member-a", "Live Writer A", env.owner_id, "member-a.txt"),
        ("live-member-b", "Live Writer B", shared_owner, "member-b.txt"),
        ("live-team-host", "Live Team Coordinator", env.owner_id, ""),
    ):
        config: dict[str, Any] = {
            "workspace_dir": str(env.home / "source-experts" / expert_id),
            "memory": {"memory_enabled": False},
        }
        if filename:
            prompt = (
                f"你是{name}。接到委派后实际调用read_file读取当前项目的/seed.txt，"
                f"再实际调用write_file把seed中的nonce原样写入/{filename}。"
                "必须自己先读再写，不猜测内容。不要执行命令，不浏览网络，不请求用户确认。"
                "完成后简短报告已读写的路径，不输出其他专家的私有配置。"
            )
        else:
            config["tools_disabled"] = ["read_file", "write_file", "edit_file", "execute"]
            prompt = (
                "你是团队协调者。必须先agent_list，再分别同步ask_agent委派Live Writer A和"
                "Live Writer B实际完成各自项目文件读写。不要自己操作文件，不要只描述计划。"
                "所有成员只在当前项目目录工作。等待两位成员完成后简短汇总，不要重复委派。"
            )
        services.agent_repo.create(
            agent_id=expert_id,
            user_id=owner,
            name=name,
            kind="team" if not filename else "expert",
            system_prompt=prompt,
            config_json=json.dumps(config),
            default_model=env.model_ref,
        )
        workspace = _workspace(services, services.agent_repo.get(expert_id), config)
        workspace.upload_bytes("SOUL.md", f"Public task persona: {name}".encode())
        workspace.upload_bytes("USER.md", private_marker.encode())
        if not filename:
            workspace.upload_bytes(
                ".octop/manifest.json", b'{"members":["live-member-a","live-member-b"]}'
            )
    services.agent_repo.set_shared("live-member-b", True)
    for expert_id in ("live-member-a", "live-member-b", "live-team-host"):
        original[expert_id] = services.agent_repo.get(expert_id)
    return original, private_marker


async def test_live_team_uses_both_members_and_keeps_shared_permissions(live_task_env):
    env = live_task_env
    created_user = _require(
        await env.client.post(
            "/api/users",
            headers=env.auth,
            json={"username": "live-team-sharer", "password": "LiveTeamUser42!", "role": "user"},
        ),
        201,
    )
    shared_owner = created_user["id"]
    other_auth = await auth_header(
        env.client, username="live-team-sharer", password="LiveTeamUser42!"
    )
    original, private_marker = await asyncio.to_thread(_seed_team, env, shared_owner)
    directory = env.home / "team-project"
    await asyncio.to_thread(directory.mkdir)
    nonce = "team-nonce-" + uuid4().hex
    await asyncio.to_thread((directory / "seed.txt").write_text, nonce, encoding="utf-8")
    project = _require(
        await env.client.post(
            "/api/projects",
            headers={**env.auth, "Idempotency-Key": str(uuid4())},
            json={
                "name": "Real team acceptance",
                "directory": str(directory),
                "instructions": "仅在当前项目目录完成明确请求的读写；不调用execute或网络工具。",
            },
        ),
        201,
    )
    resources = _require(
        await env.client.get("/api/project-tasks/resources", headers=env.auth), 200
    )
    options = {item["expert_id"]: item for item in resources["experts"]}
    assert options["live-team-host"]["member_ids"] == ["live-member-a", "live-member-b"]
    assert options["live-member-b"]["is_shared"] and not options["live-member-b"]["is_owner"]
    accepted = _require(
        await env.client.post(
            "/api/project-tasks",
            headers=env.auth,
            json={
                "project_id": project["project_id"],
                "selected_expert_id": "live-team-host",
                "request_id": "live-team-" + uuid4().hex,
                "action_mode": "craft",
                "web_search_enabled": False,
                "prompt": "现在实际完成团队协作：先agent_list，再用ask_agent同步委派Live Writer A读取/seed.txt，把其中nonce原样写入/member-a.txt；同时委派Live Writer B独立读取/seed.txt，把其中nonce原样写入/member-b.txt。两位成员各自使用read_file和write_file，不要由你代做。收到两位完成结果后简短汇总。无需用户确认，不要执行命令或网络访问。",
                "options": live_options(env),
            },
        ),
        202,
    )
    task = await await_task(env, accepted["task_id"], timeout=240)
    turn = task["turns"][-1]
    assert task["status"] == "completed", (
        task["status"],
        turn.get("error_code"),
        turn.get("recovery"),
    )
    for filename in ("member-a.txt", "member-b.txt"):
        assert nonce in await asyncio.to_thread((directory / filename).read_text, encoding="utf-8")
    members = {"live-member-a", "live-member-b"}
    blocks = turn["content_blocks"]
    assert members <= {item.get("speaker_agent_id") for item in blocks if item["type"] == "text"}
    for member in members:
        calls = [
            item
            for item in blocks
            if item["type"] == "tool"
            and item.get("speaker_agent_id") == member
            and item["status"] == "completed"
        ]
        assert {"read_file", "write_file"} <= {item["tool_name"] for item in calls}, (
            member,
            [(item["tool_name"], item["status"]) for item in calls],
        )
    assert turn["input_tokens"] > 0 and turn["output_tokens"] > 0
    receipts = env.server.services.model_call_repo.list_page(
        task_id=task["task_id"], turn_id=turn["turn_id"], purpose="team_member", limit=100
    )
    assert members <= {row.agent_id for row in receipts}
    assert all(
        row.user_id == env.owner_id and row.project_id == project["project_id"] for row in receipts
    )
    assert all(row.status == "succeeded" for row in receipts)
    source_rows = {
        expert_id: env.server.services.agent_repo.get(expert_id) for expert_id in original
    }
    assert source_rows == original
    for row in source_rows.values():
        workspace = _workspace(env.server.services, row, json.loads(row.config_json))
        assert not await workspace.aexists("member-a.txt")
        assert not await workspace.aexists("member-b.txt")
    saved = env.server.services.project_task_turn_repo.get(turn["turn_id"])
    shared_profile = next(
        item
        for item in json.loads(saved.configuration_json)["expert_profile"]["team"]["members"]
        if item["expert_id"] == "live-member-b"
    )
    assert "USER.md" not in {item["path"] for item in shared_profile["profile_files"]}
    assert private_marker not in json.dumps(blocks)
    denied = await env.client.get(f"/api/project-tasks/{task['task_id']}", headers=other_auth)
    assert denied.status_code == 404
    env.server.services.agent_repo.set_shared("live-member-b", False)
    before = len(env.server.services.model_call_repo.list_page(task_id=task["task_id"], limit=100))
    revoked = await env.client.post(
        f"/api/project-tasks/{task['task_id']}/messages",
        headers=env.auth,
        json={
            "request_id": "revoked-team-" + uuid4().hex,
            "prompt": "再执行一次同样的团队操作",
            "options": live_options(env),
        },
    )
    assert revoked.status_code == 404, revoked.status_code
    after = len(env.server.services.model_call_repo.list_page(task_id=task["task_id"], limit=100))
    assert after == before
    evidence = {
        "task_id": task["task_id"],
        "turn_id": turn["turn_id"],
        "status": task["status"],
        "member_ids": sorted(members),
        "member_calls": len(receipts),
        "input_tokens": turn["input_tokens"],
        "output_tokens": turn["output_tokens"],
        "project_files_verified": True,
        "ownership_denial": denied.status_code,
        "revoked_share_denial": revoked.status_code,
    }
    await asyncio.to_thread(
        (env.home / "team-live-evidence.json").write_text,
        json.dumps(evidence, indent=2),
        encoding="utf-8",
    )
