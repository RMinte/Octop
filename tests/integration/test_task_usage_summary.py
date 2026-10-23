"""Task usage APIs expose private source totals without inventing missing consumption."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from octop.infra.db.repos.model_calls import ModelCallRow, ModelCallSource, ModelCallStatus
from tests.support.auth import create_user, resolve_user_id

_WHEN = int(datetime(2026, 10, 10, tzinfo=UTC).timestamp() * 1000)


def _tokens(input_tokens: int, output_tokens: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "uncached_input_tokens": input_tokens,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def _historical_provider(server: Any, name: str, kind: str) -> None:
    created_at = _WHEN // 1000 - 86_400
    with server.services.db.transaction() as conn:
        conn.execute(
            "INSERT INTO providers(name,kind,created_at,updated_at) VALUES(?,?,?,?)",
            (name, kind, created_at, created_at),
        )


def _task(
    server: Any,
    owner: int,
    task_id: str,
    *,
    request_id: str | None = None,
    project_id: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model_ref: str = "fixture/model",
) -> None:
    with server.services.db.transaction() as conn:
        if project_id is not None:
            conn.execute(
                "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO NOTHING",
                (project_id, owner, "Project " + project_id, _WHEN, _WHEN),
            )
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,project_id,title,request_id,status,"
            "created_at,updated_at) VALUES(?,?,?,?,?,'completed',?,?)",
            (task_id, owner, project_id, "Task " + task_id, "create-" + task_id, _WHEN, _WHEN),
        )
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
            "configuration_json,model_ref,created_at,updated_at,input_tokens,output_tokens) "
            "VALUES(?,?,?,'Prompt','completed','{}',?,?,?,?,?)",
            (
                "turn-" + task_id,
                task_id,
                request_id or "request-" + task_id,
                model_ref,
                _WHEN,
                _WHEN,
                input_tokens,
                output_tokens,
            ),
        )


def _call(
    server: Any,
    owner: int,
    call_id: str,
    *,
    task_id: str = "",
    submission_id: str = "",
    usage: dict[str, int] | None = None,
    source: ModelCallSource = "box",
    status: ModelCallStatus = "succeeded",
    started_at: int = _WHEN,
    purpose: str = "main",
) -> None:
    server.services.model_call_repo.finish(
        ModelCallRow(
            call_id=call_id,
            started_at=started_at,
            finished_at=started_at + 1,
            user_id=owner,
            task_id=task_id,
            submission_id=submission_id,
            source=source,
            status=status,
            purpose=purpose,
            provider="fixture",
            model_ref="fixture/model",
            usage_json=json.dumps(usage or {}),
        )
    )


async def _summary(
    client: Any,
    auth: dict[str, str],
    *,
    admin: bool = False,
    **params: Any,
) -> dict[str, Any]:
    response = await client.get(
        "/api/admin/usage/task-summary" if admin else "/api/usage/task-summary",
        headers=auth,
        params={"window": "month:2026-10", **params},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_task_summary_requires_auth_and_isolates_personal_and_admin_scopes(env: Any) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    alice_auth = await create_user(client, auth, username="task-usage-alice")
    alice = await resolve_user_id(client, auth, "task-usage-alice")
    _task(server, owner, "admin-task", request_id="shared-request")
    _task(server, alice, "alice-task", request_id="shared-request")
    _call(server, owner, "admin-routing", submission_id="shared-request", usage=_tokens(10, 1))
    _call(
        server,
        alice,
        "alice-routing",
        submission_id="shared-request",
        source="third_party",
        usage=_tokens(20, 2),
    )

    assert (await client.get("/api/usage/task-summary")).status_code == 401
    assert (
        await client.get("/api/admin/usage/task-summary", headers=alice_auth)
    ).status_code == 403
    own = await _summary(client, alice_auth, user_id=owner)
    assert own["account_scope"] == "user" and own["user_id"] == alice
    assert [item["task_id"] for item in own["items"]] == ["alice-task"]
    assert own["totals"]["call_usage"]["tokens"]["total_tokens"] == 22
    assert own["items"][0]["call_usage"]["attribution_counts"] == {"submission": 1}
    assert (await _summary(client, auth))["totals"]["call_usage"]["tokens"]["total_tokens"] == 11
    all_users = await _summary(client, auth, admin=True)
    assert all_users["account_scope"] == "all_users" and all_users["user_id"] is None
    assert {item["task_id"] for item in all_users["items"]} == {"alice-task", "admin-task"}
    assert all_users["totals"]["call_usage"]["tokens"]["total_tokens"] == 33
    scoped = await _summary(client, auth, admin=True, user_id=alice)
    assert scoped["totals"] == own["totals"]
    assert (await _summary(client, alice_auth, task_id="admin-task"))["items"] == []


async def test_cancelled_missing_usage_stays_unknown_and_reported_zero_stays_zero(env: Any) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    _task(server, owner, "unknown")
    _task(server, owner, "zero")
    _call(server, owner, "cancelled", task_id="unknown", status="cancelled")
    _call(server, owner, "zero", task_id="zero", source="third_party", usage=_tokens(0, 0))

    page = await _summary(client, auth)
    by_id = {item["task_id"]: item for item in page["items"]}
    unknown = by_id["unknown"]["call_usage"]
    assert unknown["calls"] == 1 and unknown["statuses"] == {"cancelled": 1}
    assert unknown["tokens"]["input_tokens"] is None
    assert unknown["tokens"]["total_tokens"] is None
    assert unknown["missing_calls"]["total_tokens"] == 1
    assert unknown["coverage"]["total_tokens"] == "not_collected"
    assert unknown["reference_cost"]["amount_usd"] is None
    assert unknown["reference_cost"]["status"] == "unknown"
    zero = by_id["zero"]["call_usage"]
    assert zero["tokens"]["input_tokens"] == 0 and zero["tokens"]["total_tokens"] == 0
    assert zero["missing_calls"]["total_tokens"] == 0
    assert zero["coverage"]["total_tokens"] == "recorded"
    assert zero["reference_cost"]["amount_usd"] == "0"
    assert zero["reference_cost"]["status"] == "estimated"
    totals = page["totals"]["call_usage"]
    assert totals["calls"] == 2 and totals["tokens"]["total_tokens"] == 0
    assert totals["coverage"]["total_tokens"] == "partial"
    assert totals["missing_calls"]["total_tokens"] == 1
    assert page["totals"]["sources"]["box"]["tokens"]["total_tokens"] is None
    assert page["totals"]["sources"]["third_party"]["tokens"]["total_tokens"] == 0


async def test_pagination_keeps_whole_range_totals_and_unassigned_calls_separate(env: Any) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    for index, (task_id, source) in enumerate(
        (("first", "box"), ("second", "third_party"), ("third", "box")), start=1
    ):
        _task(server, owner, task_id)
        _call(
            server,
            owner,
            task_id,
            task_id=task_id,
            source=source,
            usage=_tokens(index * 10, 0),
            started_at=_WHEN + index,
        )
    _call(server, owner, "rejected", submission_id="not-accepted", usage=_tokens(100, 0))

    first = await _summary(client, auth, limit=1)
    second = await _summary(client, auth, limit=1, offset=1)
    third = await _summary(client, auth, limit=1, offset=2)
    beyond = await _summary(client, auth, limit=1, offset=3)
    assert [page["items"][0]["task_id"] for page in (first, second, third)] == [
        "third",
        "second",
        "first",
    ]
    assert first["total_tasks"] == second["total_tasks"] == third["total_tasks"] == 3
    assert first["next_offset"] == 1 and second["next_offset"] == 2
    assert third["next_offset"] is None
    assert beyond["items"] == [] and beyond["next_offset"] is None
    assert all(page["totals"] == first["totals"] for page in (second, third, beyond))
    assert first["totals"]["call_usage"]["calls"] == 3
    assert first["totals"]["call_usage"]["tokens"]["total_tokens"] == 60
    assert first["totals"]["sources"]["box"]["tokens"]["total_tokens"] == 40
    assert first["totals"]["sources"]["third_party"]["tokens"]["total_tokens"] == 20
    assert first["unassigned_call_usage"]["calls"] == 1
    assert first["unassigned_call_usage"]["tokens"]["total_tokens"] == 100
    assert first["unassigned_sources"]["box"]["calls"] == 1
    assert first["pricing"]["model"] == "gpt-6-astra"
    assert first["pricing"]["basis"] == "openai_reference_estimate"
    assert first["capabilities"]["actual_cost"] is False
    for group in (
        first["totals"]["call_usage"],
        first["totals"]["sources"]["box"],
        first["totals"]["sources"]["third_party"],
        first["unassigned_call_usage"],
    ):
        assert group["reference_cost"]["status"] == "estimated"
        assert group["reference_cost"]["amount_usd"] is not None


async def test_submission_attribution_requires_unique_accepted_task_before_filtering(
    env: Any,
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    _task(server, owner, "accepted", request_id="accepted-request", project_id="first-project")
    _task(server, owner, "ambiguous-first", request_id="ambiguous", project_id="first-project")
    _task(server, owner, "ambiguous-second", request_id="ambiguous", project_id="second-project")
    _call(
        server,
        owner,
        "routing",
        submission_id="accepted-request",
        purpose="difficulty",
        usage=_tokens(3, 0),
    )
    _call(
        server,
        owner,
        "direct",
        task_id="accepted",
        submission_id="ambiguous",
        source="third_party",
        usage=_tokens(5, 0),
    )
    _call(server, owner, "ambiguous", submission_id="ambiguous", usage=_tokens(7, 0))
    _call(server, owner, "rejected", submission_id="rejected", usage=_tokens(11, 0))

    page = await _summary(client, auth)
    assert page["total_tasks"] == 1 and page["items"][0]["task_id"] == "accepted"
    assert page["totals"]["call_usage"]["tokens"]["total_tokens"] == 8
    assert page["totals"]["call_usage"]["attribution_counts"] == {"submission": 1, "direct": 1}
    assert page["unassigned_call_usage"]["calls"] == 2
    assert page["unassigned_call_usage"]["tokens"]["total_tokens"] == 18
    for filters in ({"task_id": "accepted"}, {"project_id": "first-project"}):
        scoped = await _summary(client, auth, **filters)
        assert scoped["totals"]["call_usage"]["tokens"]["total_tokens"] == 8
        assert scoped["unassigned_call_usage"]["calls"] == 0
        assert scoped["items"][0]["project_name"] == "Project first-project"
    assert (await _summary(client, auth, task_id="ambiguous-first"))["items"] == []


async def test_retained_task_tokens_are_visible_without_ledger_and_never_added_twice(
    env: Any,
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    _historical_provider(server, "HistoricalThird", "openai")
    _task(
        server,
        owner,
        "overlap",
        input_tokens=100,
        output_tokens=10,
        model_ref="HistoricalThird/model",
    )
    _task(
        server,
        owner,
        "old-only",
        input_tokens=40,
        output_tokens=5,
        model_ref="HistoricalThird/model",
    )
    before = await client.get(
        "/api/usage/summary", headers=auth, params={"window": "month:2026-10"}
    )
    assert before.status_code == 200 and before.json()["total_tokens"] == 155
    _call(server, owner, "overlap", task_id="overlap", source="third_party", usage=_tokens(100, 10))

    page = await _summary(client, auth)
    assert page["total_tasks"] == 2
    assert page["totals"]["call_usage"]["tokens"]["total_tokens"] == 110
    assert page["totals"]["retained_task_usage"]["total_tokens"] == 155
    assert page["totals"]["sources"]["third_party"]["tokens"]["total_tokens"] == 110
    assert page["totals"]["retained_task_usage"]["sources"]["third_party"]["total_tokens"] == 155
    assert page["totals"]["retained_task_usage"]["cannot_add_to_call_usage"] is True
    old = next(item for item in page["items"] if item["task_id"] == "old-only")
    assert old["call_usage"]["calls"] == 0
    assert old["call_usage"]["tokens"]["total_tokens"] is None
    assert old["retained_task_usage"]["total_tokens"] == 45
    assert old["retained_task_usage"]["coverage"] == "recorded"
    after = await client.get("/api/usage/summary", headers=auth, params={"window": "month:2026-10"})
    assert after.status_code == 200 and after.json()["total_tokens"] == 155


async def test_retained_task_infers_each_turn_source_when_its_model_changes(env: Any) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    _historical_provider(server, "HistoricalBox", "box")
    _historical_provider(server, "HistoricalThird", "openai")
    _task(
        server,
        owner,
        "mixed-history",
        input_tokens=100,
        output_tokens=10,
        model_ref="HistoricalBox/model",
    )
    with server.services.db.transaction() as conn:
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
            "configuration_json,model_ref,created_at,updated_at,input_tokens,output_tokens) "
            "VALUES('followup','mixed-history','followup','Prompt','completed','{}',"
            "'HistoricalThird/model',?,?,50,5)",
            (_WHEN + 1_000, _WHEN + 1_000),
        )

    page = await _summary(client, auth, task_id="mixed-history")

    item = page["items"][0]
    retained = item["retained_task_usage"]
    assert retained["source_attribution"] == "inferred"
    assert retained["total_tokens"] == 165 and retained["turns"] == 2
    assert retained["sources"]["box"]["total_tokens"] == 110
    assert retained["sources"]["third_party"]["total_tokens"] == 55
    assert retained["sources"]["box"]["source_basis"] == "configuration_inference"
    assert retained["sources"]["third_party"]["source_basis"] == "configuration_inference"
    assert retained["unattributed_usage"]["total_tokens"] is None
    assert retained["reference_cost"]["status"] == "estimated"
    assert retained["reference_cost"]["amount_usd"] == "0.00225"
    assert item["call_usage"]["calls"] == 0
    for source in ("box", "third_party"):
        assert item["sources"][source]["tokens"]["total_tokens"] is None
        assert item["sources"][source]["coverage"]["total_tokens"] == "no_data"


async def test_old_third_party_task_has_inferred_tokens_and_cost_without_fabricated_calls(
    env: Any,
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    _historical_provider(server, "HistoricalThird", "openai")
    _task(
        server,
        owner,
        "old-third-party",
        input_tokens=12_345,
        output_tokens=456,
        model_ref="HistoricalThird/model",
    )

    page = await _summary(client, auth)

    assert page["total_tasks"] == 1
    item = page["items"][0]
    retained = item["retained_task_usage"]
    assert retained["source_attribution"] == "inferred"
    assert retained["total_tokens"] == 12_801
    assert retained["sources"]["third_party"]["total_tokens"] == 12_801
    assert retained["sources"]["box"]["total_tokens"] is None
    assert retained["sources"]["third_party"]["reference_cost"]["status"] == "estimated"
    assert retained["reference_cost"]["amount_usd"] == "0.14625"
    assert "unreported_cache_reads_assumed_uncached" in retained["reference_cost"]["assumptions"]
    assert item["call_usage"]["calls"] == 0
    assert item["sources"]["third_party"]["tokens"]["total_tokens"] is None
    assert page["totals"]["call_usage"]["tokens"]["total_tokens"] is None
    assert page["totals"]["retained_task_usage"]["total_tokens"] == 12_801


async def test_retained_large_journal_and_title_add_reference_bounds_without_ledger_overlap(
    env: Any,
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    _historical_provider(server, "HistoricalThird", "openai")
    _task(
        server,
        owner,
        "range-history",
        request_id="create-range-history",
        input_tokens=900_000,
        output_tokens=100,
        model_ref="HistoricalThird/model",
    )
    with server.services.db.transaction() as conn:
        conn.execute(
            "INSERT INTO project_task_titles(task_id,status,input_tokens,output_tokens) "
            "VALUES('range-history','completed',1000,100)"
        )
    _call(
        server,
        owner,
        "overlapping-ledger-call",
        task_id="range-history",
        source="third_party",
        usage=_tokens(900_000, 100),
    )

    page = await _summary(client, auth)

    assert page["total_tasks"] == 1
    item = page["items"][0]
    retained = item["retained_task_usage"]
    cost = retained["reference_cost"]
    assert retained["usage_records"] == 2 and retained["turns"] == 1
    assert retained["total_tokens"] == 901_200
    assert retained["sources"]["third_party"]["total_tokens"] == 901_200
    assert retained["source_attribution"] == "inferred"
    assert cost["status"] == "estimated_range"
    assert cost["amount_usd"] is None
    assert cost["known_amount_usd"] == "0.015"
    assert cost["lower_bound_usd"] == "9.02"
    assert cost["upper_bound_usd"] == "18.0225"
    assert cost["estimated_records"] == 1 and cost["estimated_range_records"] == 1
    assert cost["unknown_records"] == 0
    assert cost["missing_fields"] == ["per_call_context"]
    assert "retained_context_band_range" in cost["assumptions"]
    assert retained["sources"]["third_party"]["reference_cost"] == cost
    assert page["totals"]["retained_task_usage"]["reference_cost"] == cost
    assert retained["cannot_add_to_call_usage"] is True
    # The receipt is one known invocation; the journal remains an unknown mix.
    assert item["call_usage"]["tokens"]["total_tokens"] == 900_100
    assert item["call_usage"]["reference_cost"]["amount_usd"] == "18.0075"
    assert item["call_usage"]["reference_cost"]["estimated_range_calls"] == 0


async def test_missing_historical_provider_keeps_known_tokens_unattributed(env: Any) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    _task(
        server,
        owner,
        "unknown-provider",
        input_tokens=21,
        output_tokens=3,
        model_ref="MissingProvider/model",
    )

    page = await _summary(client, auth)

    retained = page["items"][0]["retained_task_usage"]
    assert retained["source_attribution"] == "unknown"
    assert retained["total_tokens"] == 24
    assert retained["unattributed_usage"]["total_tokens"] == 24
    assert retained["unattributed_usage"]["source_basis"] == "unattributed"
    assert retained["sources"]["box"]["total_tokens"] is None
    assert retained["sources"]["third_party"]["total_tokens"] is None
    assert retained["reference_cost"]["status"] == "estimated"
    assert page["totals"]["retained_task_usage"]["unattributed_usage"]["total_tokens"] == 24
    assert page["totals"]["call_usage"]["calls"] == 0


async def test_empty_summary_reports_no_data_instead_of_known_zero_consumption(env: Any) -> None:
    client, _server, auth = env

    page = await _summary(client, auth)

    assert page["items"] == [] and page["total_tasks"] == 0 and page["next_offset"] is None
    assert page["totals"]["call_usage"]["calls"] == 0
    assert page["totals"]["call_usage"]["tokens"]["total_tokens"] is None
    assert page["totals"]["call_usage"]["coverage"]["total_tokens"] == "no_data"
    assert page["totals"]["call_usage"]["missing_calls"]["total_tokens"] == 0
    assert page["totals"]["call_usage"]["reference_cost"]["status"] == "no_data"
    assert page["totals"]["retained_task_usage"]["total_tokens"] is None
    assert page["unassigned_call_usage"]["calls"] == 0
    for source in ("box", "third_party"):
        assert page["totals"]["sources"][source]["tokens"]["total_tokens"] is None
    assert page["history_complete"] is False


async def test_calendar_windows_use_server_timezone_and_half_open_millisecond_boundaries(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    monkeypatch.setattr(server, "config", replace(server.config, default_timezone="Asia/Shanghai"))
    start = int(datetime(2026, 10, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    end = int(datetime(2026, 10, 2, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    for call_id, timestamp, count in (
        ("before", start - 1, 1),
        ("start", start, 10),
        ("last", end - 1, 20),
        ("end", end, 100),
    ):
        _call(server, owner, call_id, task_id="task", started_at=timestamp, usage=_tokens(count, 0))

    for window in ("day:2026-10-01", "range:2026-10-01:2026-10-01"):
        page = await _summary(client, auth, window=window)
        assert page["timezone"] == "Asia/Shanghai"
        assert page["range_start"] == start and page["range_end"] == end
        assert page["totals"]["call_usage"]["calls"] == 2
        assert page["totals"]["call_usage"]["tokens"]["total_tokens"] == 30


async def test_task_summary_rejects_invalid_windows_and_pagination(env: Any) -> None:
    client, _server, auth = env
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": "invalid"}):
        response = await client.get("/api/usage/task-summary", headers=auth, params=params)
        assert response.status_code == 422, response.text
    for window in ("unexpected", "day:2026-02-30", "month:2026-13", "range:2026-10-02:2026-10-01"):
        response = await client.get(
            "/api/usage/task-summary", headers=auth, params={"window": window}
        )
        assert response.status_code == 400, response.text
    response = await client.get(
        "/api/admin/usage/task-summary", headers=auth, params={"user_id": 0}
    )
    assert response.status_code == 422, response.text
