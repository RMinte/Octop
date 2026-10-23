"""Real Task -> extractor -> canonical memory -> fresh Task acceptance."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from uuid import uuid4

import pytest
from tests.live.unified_task_support import LiveTaskEnvironment, await_task, live_options

from octop.infra.projects.expert_memory import ExpertMemory
from octop.infra.projects.expert_profiles import _workspace

pytestmark = pytest.mark.live


def _safe_error(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    return {
        "status": response.status_code,
        "code": error.get("code") if isinstance(error, dict) else None,
    }


async def _new_task(
    env: LiveTaskEnvironment, expert_id: str, project: dict[str, Any], prompt: str
) -> dict[str, Any]:
    response = await env.client.post(
        "/api/project-tasks",
        headers=env.auth,
        json={
            "request_id": str(uuid4()),
            "selected_expert_id": expert_id,
            "project_id": project["project_id"],
            "expected_project_revision": project["revision"],
            "action_mode": "ask",
            "prompt": prompt,
            "web_search_enabled": False,
            "options": {
                **live_options(env),
                "skills": [],
                "mcp_servers": [],
                "plugins": [],
                "subagents": [],
                "knowledge_base_ids": [],
            },
        },
    )
    assert response.status_code == 202, _safe_error(response)
    task = await await_task(env, response.json()["task_id"])
    turn = task["turns"][-1]
    assert turn["status"] == "completed", {
        "status": turn["status"],
        "error_code": turn.get("error_code"),
    }
    return task


async def _capture_policy(env: LiveTaskEnvironment, task_id: str, enabled: bool) -> None:
    path = f"/api/project-tasks/{task_id}/expert-memory/policy"
    response = await env.client.get(path, headers=env.auth)
    assert response.status_code == 200, _safe_error(response)
    policy = response.json()
    response = await env.client.patch(
        path,
        headers=env.auth,
        json={
            "expected_revision": policy["config_revision"],
            "expected_expert_id": policy["expert_id"],
            "legacy_recall_enabled": False,
            "capture_enabled": enabled,
            "auto_extract_enabled": True,
            "general_promotion_enabled": True,
        },
    )
    assert response.status_code == 200, _safe_error(response)


async def _await_job(env: LiveTaskEnvironment, task_id: str) -> Any:
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        jobs = await asyncio.to_thread(
            env.server.services.expert_memory_job_repo.list_for_task, task_id, env.owner_id
        )
        if jobs and jobs[0].status in {"completed", "failed", "cancelled", "uncertain"}:
            job = jobs[0]
            assert job.status == "completed", {
                "job_id": job.job_id,
                "status": job.status,
                "phase": job.phase,
                "error_code": job.error_code,
            }
            return job
        await asyncio.sleep(0.25)
    raise TimeoutError("Real-model expert-memory job did not reach a terminal state")


def _memory(env: LiveTaskEnvironment, task: dict[str, Any]) -> ExpertMemory:
    turn_id = task["turns"][-1]["turn_id"]
    turn = env.server.services.project_task_turn_repo.get(turn_id)
    return ExpertMemory(
        env.server.services,
        env.owner_id,
        task["task_id"],
        turn_id,
        task["project_id"],
        json.loads(turn.configuration_json)["expert_profile"],
        enabled=True,
    )


def _canonical(store: Any, marker: str) -> dict[str, Any]:
    atoms = [atom for atom in store.list_atoms(limit=100) if marker in atom.assertion]
    assert atoms, {
        "reason": "no_canonical_atom",
        "candidate_count": len(store.list_candidates(limit=100)),
    }
    atom = atoms[0]
    candidate = store.get_candidate(atom.candidate_id)
    evidence = store.get_raw(atom.quote_event_id)
    assert candidate is not None and evidence is not None
    return {
        "atom_id": atom.id,
        "candidate_id": candidate.id,
        "event_id": evidence.id,
        "assertion": atom.assertion,
        "quote": atom.verbatim_quote,
        "evidence": evidence.content,
        "candidate_payload": candidate.payload,
        "event_payload": evidence.payload,
    }


async def _recall_snapshot(memory: ExpertMemory) -> str:
    payload = await asyncio.to_thread(memory.journal_path("recall").read_text, encoding="utf-8")
    return str(json.loads(payload)["content"])


async def test_real_model_extraction_canonical_recall_and_general_scope_isolation(
    live_task_env: LiveTaskEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    env = live_task_env
    services = env.server.services
    nonce = uuid4().hex[:10]
    expert_id = "memory-live-" + nonce
    project_marker, project_secret = "ORION" + nonce, "cobalt" + nonce
    preference_marker = "amber" + nonce
    report: dict[str, Any] = {"model_ref": env.model_ref, "steps": []}
    report_path = env.home.parent / "memory-live-report.json"

    async def progress(stage: str, **facts: Any) -> None:
        report["steps"].append({"stage": stage, **facts})
        await asyncio.to_thread(
            report_path.write_text,
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with capsys.disabled():
            print(
                "LIVE_MEMORY " + json.dumps({"stage": stage, **facts}, ensure_ascii=False),
                flush=True,
            )

    expert_home = env.home / "memory-acceptance-expert"
    config = {
        "workspace_dir": str(expert_home),
        "memory": {
            "memory_enabled": True,
            "capture_enabled": True,
            "auto_extract_enabled": True,
            "general_promotion_enabled": True,
            "legacy_recall_enabled": False,
        },
    }
    services.agent_repo.create(
        agent_id=expert_id,
        user_id=env.owner_id,
        name="Live memory expert",
        system_prompt="收到可记忆信息时简短确认；被问及既有信息时只依据可用记忆，没有证据时明确未知。",
        default_model=env.model_ref,
        config_json=json.dumps(config),
    )
    workspace = _workspace(services, services.agent_repo.get(expert_id), config)
    await workspace.aupload_bytes("SOUL.md", "保持来源准确，区分项目事实与用户通用偏好。".encode())
    projects = []
    for name in ("Alpha", "Beta"):
        directory = env.home / ("project-" + name.lower())
        await asyncio.to_thread(directory.mkdir, parents=True)
        response = await env.client.post(
            "/api/projects",
            headers={**env.auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Memory " + name, "directory": str(directory)},
        )
        assert response.status_code == 201, _safe_error(response)
        projects.append(response.json())
    await progress("isolated_environment_ready", expert_id=expert_id)

    learned = await _new_task(
        env,
        expert_id,
        projects[0],
        f"请记住本项目的已确认事实：项目代号是{project_marker}，项目验收口令是{project_secret}。这条事实只属于本项目，不是通用偏好。简短确认即可。",
    )
    await progress(
        "project_task_completed",
        task_id=learned["task_id"],
        turn_id=learned["turns"][-1]["turn_id"],
    )
    job = await _await_job(env, learned["task_id"])
    learned_memory = _memory(env, learned)
    scoped = await asyncio.to_thread(
        learned_memory.management_operation,
        "scope",
        lambda store: _canonical(store, project_secret),
    )
    assert scoped["candidate_payload"]["extraction_job_id"] == job.job_id
    assert scoped["event_payload"]["source_turn_id"] == learned["turns"][-1]["turn_id"]
    assert scoped["quote"] in scoped["evidence"]
    await progress(
        "project_scope_atom_created",
        job_id=job.job_id,
        atom_id=scoped["atom_id"],
        candidate_id=scoped["candidate_id"],
    )
    await _capture_policy(env, learned["task_id"], False)

    recalled = await _new_task(
        env,
        expert_id,
        projects[0],
        f"请从已有记忆回答{project_marker}的项目验收口令。只回答口令；不知道就回答未知，不要猜测。",
    )
    snapshot = await _recall_snapshot(_memory(env, recalled))
    assert project_secret in snapshot and project_secret in recalled["turns"][-1]["reply"]
    assert recalled["task_id"] != learned["task_id"]
    await progress("fresh_task_recalled_project_fact", task_id=recalled["task_id"])

    isolated = await _new_task(
        env,
        expert_id,
        projects[1],
        f"请从当前可用记忆回答{project_marker}的项目验收口令。没有可用证据就回答未知，不要猜测。",
    )
    snapshot = await _recall_snapshot(_memory(env, isolated))
    assert project_secret not in snapshot and project_secret not in isolated["turns"][-1]["reply"]
    response = await env.client.get(
        f"/api/project-tasks/{isolated['task_id']}/expert-memory",
        headers=env.auth,
        params={"source": "general"},
    )
    assert response.status_code == 200, _safe_error(response)
    assert not any(project_secret in entry["content"] for entry in response.json()["entries"])
    await progress("other_project_and_general_are_isolated", task_id=isolated["task_id"])

    await _capture_policy(env, learned["task_id"], True)
    preference_sentence = f"我在所有项目中都偏好使用{preference_marker}作为示例标签。"
    preferred = await _new_task(env, expert_id, projects[0], preference_sentence)
    preference_job = await _await_job(env, preferred["task_id"])
    preference_memory = _memory(env, preferred)
    general = await asyncio.to_thread(
        preference_memory.management_operation,
        "general",
        lambda store: _canonical(store, preference_marker),
    )
    assert general["candidate_payload"]["extraction_job_id"] == preference_job.job_id
    assert general["candidate_payload"]["promotion_rule"] == "explicit_universal_user_preference_v1"
    assert general["candidate_payload"]["source_namespace"] == learned_memory.namespaces["scope"]
    assert general["event_payload"]["source_turn_id"] == preferred["turns"][-1]["turn_id"]
    assert general["evidence"] == general["quote"] and general["quote"] in preference_sentence
    assert project_secret not in general["evidence"]
    await progress(
        "general_preference_atom_created",
        job_id=preference_job.job_id,
        atom_id=general["atom_id"],
        candidate_id=general["candidate_id"],
    )
    await _capture_policy(env, preferred["task_id"], False)

    global_recalled = await _new_task(
        env,
        expert_id,
        projects[1],
        "我的示例标签偏好是什么？请根据已有记忆原样回答标签，没有证据就回答未知。",
    )
    snapshot = await _recall_snapshot(_memory(env, global_recalled))
    assert (
        preference_marker in snapshot and preference_marker in global_recalled["turns"][-1]["reply"]
    )
    assert project_secret not in snapshot
    with services.db.connect() as conn:
        calls = conn.execute(
            "SELECT call_id,status,usage_json FROM model_calls WHERE purpose='memory_extract' AND agent_id=?",
            (expert_id,),
        ).fetchall()
        summary = conn.execute(
            "SELECT COUNT(*) AS count,SUM(input_tokens) AS inputs,SUM(output_tokens) AS outputs FROM usage_log WHERE source='memory_extract' AND agent_id=?",
            (expert_id,),
        ).fetchone()
    assert len(calls) == 2 and all(call["status"] == "succeeded" for call in calls)
    assert summary["count"] == 2 and summary["inputs"] > 0 and summary["outputs"] > 0
    await progress(
        "cross_project_general_recall_and_usage_verified",
        task_id=global_recalled["task_id"],
        memory_model_calls=len(calls),
        input_tokens=summary["inputs"],
        output_tokens=summary["outputs"],
    )
