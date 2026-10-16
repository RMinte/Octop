"""Native waits bind public requests to exact durable interrupt identities."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any, cast

from langchain_core.messages import AIMessage
from octop_harness import HarnessAgent
from octop_harness.llm.session_header import session_header_scope
from octop_harness.request import ChatRequest

from octop.i18n import tr
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import approval_runtime, interactions
from octop.infra.projects.client_tools import ClientToolBroker, _load
from octop.infra.projects.control import ASK_USER
from octop.infra.projects.interaction_models import ApprovalPayload, AskQuestions
from octop.infra.projects.stream import TaskStreamProtocol


async def resume(
    agent: HarnessAgent, request: ChatRequest, decisions: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    protocol = cast(TaskStreamProtocol, agent.protocol)
    with session_header_scope(request.thread_id):
        async with aclosing(protocol.resume(decisions, request.to_runnable_config())) as events:
            async for event in events:
                yield event


async def checkpoint(agent: HarnessAgent, turn_id: str, expected: str = "") -> str:
    saver = agent.checkpointer
    saved = await saver.aget_tuple({"configurable": {"thread_id": turn_id}}) if saver else None
    identifier = str(saved.config["configurable"].get("checkpoint_id") or "") if saved else ""
    if not identifier or (expected and identifier != expected):
        raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
    return identifier


async def prepare_wait(
    agent: HarnessAgent, broker: ClientToolBroker, pending: list[dict[str, Any]], locale: str
) -> dict[str, Any]:
    identifier = await checkpoint(agent, broker.turn.turn_id)
    state = await agent.protocol._graph.aget_state(
        {"configurable": {"thread_id": broker.turn.turn_id}}
    )
    messages = await agent.aget_history(broker.turn.turn_id, limit=1_000_000)
    last = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    calls = list(last.tool_calls) if last else []
    # ToolNode fan-out retains each call's original index even when tool names
    # and arguments are identical. Global after_model interrupts use list order.
    interrupted_calls = {
        entry.id: calls[task.path[1]]
        for task in state.tasks
        if task.name == "tools"
        and len(task.path) >= 2
        and task.path[0] == "__pregel_push"
        and isinstance(task.path[1], int)
        and 0 <= task.path[1] < len(calls)
        for entry in task.interrupts
    }
    definitions = {"client__" + d.name: d for d in broker.definitions}
    current = await asyncio.to_thread(
        broker.services.project_task_turn_repo.get, broker.turn.turn_id
    )
    if current is None:
        raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
    items = interactions.load(current)
    old_count = len(items)
    groups, call_ids, kinds = [], [], set()
    new_approval_ids: list[str] = []
    for event in pending:
        request, interrupt_id = event["request"], event.get("interrupt_id")
        if not isinstance(interrupt_id, str) or not interrupt_id:
            raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
        entries: list[dict[str, Any]] = []
        if request.get("type") == "user_pause":
            kinds.add("user")
        else:
            actions = request.get("action_requests") or []
            if not actions:
                raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
            configs = {c["action_name"]: c for c in request.get("review_configs") or []}
            for action in actions:
                candidates = (
                    [interrupted_calls[interrupt_id]]
                    if interrupt_id in interrupted_calls
                    else calls
                )
                match = next(
                    (
                        c
                        for c in candidates
                        if c in calls
                        and c["name"] == action["name"]
                        and c["args"] == action["args"]
                    ),
                    None,
                )
                if match is None or not match.get("id"):
                    raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
                calls.remove(match)
                call_id = match["id"] or ""
                if action["name"] in definitions:
                    client_call = await asyncio.to_thread(
                        broker._create, definitions[action["name"]], action["args"], call_id
                    )
                    call_ids.append(client_call)
                    entries.append({"type": "client", "call_id": client_call})
                    kinds.add("client_tool")
                elif action["name"] == ASK_USER:
                    questions = AskQuestions.model_validate(action["args"]).questions
                    if sum(i.kind == "question" for i in items) + len(questions) > 3:
                        entries.append(
                            {
                                "type": "inline",
                                "decision": {
                                    "type": "respond",
                                    "message": tr("projects.question_limit", locale),
                                },
                            }
                        )
                        continue
                    created = [
                        interactions.new(broker.turn, question, call_id) for question in questions
                    ]
                    items.extend(created)
                    entries.append({"type": "question", "ids": [i.interaction_id for i in created]})
                    kinds.add("answer")
                else:
                    allowed = configs.get(action["name"], {}).get(
                        "allowed_decisions", ["approve", "reject"]
                    )
                    payload = ApprovalPayload(
                        tool_name=action["name"],
                        arguments=action["args"],
                        reason=tr("projects.approval_reason", locale),
                        action_digest=interactions.digest(
                            {"call": match, "configuration": broker.turn.configuration_json}
                        ),
                        allowed_decisions=["allow_once", "deny"]
                        if "approve" in allowed
                        else ["deny"],
                    )
                    item = interactions.new(broker.turn, payload, call_id)
                    items.append(item)
                    new_approval_ids.append(item.interaction_id)
                    entries.append(
                        {"type": "approval", "id": item.interaction_id, "allowed": allowed}
                    )
                    kinds.add("approval")
        groups.append({"interrupt_id": interrupt_id, "actions": entries})
    kind = next((k for k in ("user", "approval", "answer", "client_tool") if k in kinds), "answer")
    return {
        "checkpoint_id": identifier,
        "calls": call_ids,
        "groups": groups,
        "new_approval_ids": new_approval_ids,
        "wait_kind": kind,
        "interactions_json": interactions.dump(items) if len(items) != old_count else None,
    }


def resume_decisions(
    services: SharedServices, owner_id: int, turn: ProjectTaskTurnRow
) -> dict[str, Any] | None:
    user = services.user_repo.get(owner_id)
    locale = user.locale if user and user.locale else "zh"
    rows, error = {}, None
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        current = services.project_task_turn_repo.get_in_transaction(conn, turn.turn_id)
        if (
            current is None
            or current.status != "running"
            or current.cancel_requested
            or current.worker_id != turn.worker_id
            or current.checkpoint_id != turn.checkpoint_id
        ):
            raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
        wait = json.loads(current.pause_json)
        if wait["checkpoint_id"] != current.checkpoint_id:
            raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
        items = interactions.load(current)
        now = int(time.time() * 1000)
        approval_runtime.advance(services, conn, current, items, now)
        client_id = (
            json.loads(current.configuration_json)["execution_options"].get("client_id") or ""
        )
        for identifier in wait["calls"]:
            row = _load(services, conn, owner_id, client_id, turn.task_id, turn.turn_id, identifier)
            rows[identifier] = row
            if row.status == "expired":
                error = ErrorCode.TASK_CLIENT_TOOL_TIMEOUT
            elif row.status not in {"pending", "claimed", "completed"}:
                error = ErrorCode.TASK_CLIENT_TOOL_CLOSED
        kind = approval_runtime.wait_kind(
            items,
            clients=any(r.status != "completed" for r in rows.values()),
            user_paused=current.pause_requested,
        )
        if kind:
            services.project_task_repo.wait(conn, current.task_id, current.turn_id, kind, now)
    if error:
        raise OctopError.localized(error)
    if current.pause_requested:
        return None
    by_id = {i.interaction_id: i for i in items}
    result = {}
    for group in wait["groups"]:
        decisions = []
        for action in group["actions"]:
            if action["type"] == "client":
                row = rows[action["call_id"]]
                if row.status != "completed":
                    return None
                decisions.append({"type": "respond", "message": row.result_json})
            elif action["type"] == "inline":
                decisions.append(action["decision"])
            elif action["type"] == "approval":
                item = by_id[action["id"]]
                if item.status in {"pending", "reviewing"}:
                    return None
                decision = interactions.native_decision(item, locale)
                if decision["type"] not in action["allowed"]:
                    decision = (
                        {"type": "respond", "message": tr("projects.approval_not_granted", locale)}
                        if "respond" in action["allowed"]
                        else {"type": "approve"}
                    )
                decisions.append(decision)
            else:
                questions = [by_id[identifier] for identifier in action["ids"]]
                if any(q.status == "pending" for q in questions):
                    return None
                if any(q.resolution is None for q in questions):
                    raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
                decisions.append(
                    {
                        "type": "respond",
                        "message": interactions.encode(
                            {
                                "answers": [
                                    {
                                        "question": q.payload.model_dump(mode="json"),
                                        "result": q.resolution.model_dump(mode="json")
                                        if q.resolution
                                        else None,
                                    }
                                    for q in questions
                                ],
                                "authorization": False,
                            }
                        ),
                    }
                )
        result[group["interrupt_id"]] = {"decisions": decisions}
    return result
