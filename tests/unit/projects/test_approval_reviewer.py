"""Approval review isolation, durable dispatch fencing and conservative fallbacks."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_task_reviews import ProjectTaskReviewRepo, TaskReviewRow
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import approval_reviewer as reviews
from octop.infra.utils.paths import PathLayout


def job(**changes):
    original = TaskReviewRow(
        review_id="review_one",
        task_id="task_one",
        turn_id="turn_one",
        owner_id=1,
        execution_host="server",
        target_id="interaction_one",
        action_digest="fixed-digest",
        proposal_json='{"command":"read fixture","execution_host":"server"}',
        model_ref="chosen/model",
        created_at=1_000,
        updated_at=1_000,
        expires_at=601_000,
    )
    return replace(original, **changes)


@pytest.fixture
def services(tmp_path):
    db = SqlitePool(tmp_path / "reviews.db")
    run_migrations(db)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users(id,username,role,created_at) VALUES(1,'review-owner','user',1)"
        )
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,active_turn_id,created_at,updated_at) "
            "VALUES('task_one',1,'Review fixture','request_one','turn_one',1000,1000)"
        )
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,configuration_json,model_ref,created_at,updated_at) "
            "VALUES('turn_one','task_one','request_one','Read fixture','running','{}','chosen/model',1000,1000)"
        )
    try:
        yield SharedServices(PathLayout(tmp_path), OctopConfig(), RepoBundle.from_pool(db))
    finally:
        db.close()


@pytest.fixture
def isolated_model(monkeypatch):
    services = SimpleNamespace(
        user_policy_repo=object(),
        usage_repo=object(),
        user_repo=SimpleNamespace(
            get=MagicMock(return_value=SimpleNamespace(locale="zh", disabled=0))
        ),
        provider_repo=object(),
        project_task_repo=SimpleNamespace(get=MagicMock(return_value=None)),
    )
    store = MagicMock()
    store.is_model_ref_usable.return_value = True
    store.build_harness_configs.return_value = []
    monkeypatch.setattr(reviews, "ProviderStore", MagicMock(return_value=store))
    monkeypatch.setattr(reviews, "assert_token_quota_available", MagicMock())
    model = SimpleNamespace(ainvoke=AsyncMock(), _harness_max_output_tokens=2_048)
    factory = SimpleNamespace(get_chat_model=MagicMock(return_value=model), aclose=AsyncMock())
    monkeypatch.setattr(reviews, "ChatModelFactory", MagicMock(return_value=factory))
    return services, model, factory


@pytest.mark.parametrize("decision", ["allow_once", "deny", "requires_user"])
async def test_reviewer_receives_only_fixed_action_with_no_tools_or_task_context(
    isolated_model, decision
):
    services, model, factory = isolated_model
    model.ainvoke.return_value = AIMessage(
        content='{"decision":"' + decision + '"}',
        usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    )
    result = await reviews.TaskApprovalReviewer(services).generate(job())
    assert result.decision == decision
    assert (result.input_tokens, result.output_tokens) == (11, 7)
    messages = model.ainvoke.call_args.args[0]
    assert len(messages) == 2
    assert isinstance(messages[0], SystemMessage) and isinstance(messages[1], HumanMessage)
    assert messages[1].content == job().proposal_json
    assert model.ainvoke.call_args.kwargs == {"max_tokens": 2_048}
    factory.get_chat_model.assert_called_once_with("chosen/model")
    factory.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    ("content", "tool_call"),
    [
        ('{"decision":"allow_once"}', True),
        ('{"decision":"allow_once","reason":"trust me"}', False),
        ('{"decision":"deny","decision":"allow_once"}', False),
        ('```json\n{"decision":"allow_once"}\n```', False),
        ('[{"decision":"allow_once"}]', False),
        ('{"decision":"approve"}', False),
        ('{"decision":true}', False),
        (" ", False),
        (" " * 4_097 + '{"decision":"allow_once"}', False),
        ([{"type": {}, "text": '{"decision":"allow_once"}'}], False),
        ([{"type": [], "text": '{"decision":"allow_once"}'}], False),
        ("[" * 1_500 + "0" + "]" * 1_500, False),
    ],
)
async def test_reviewer_invalid_output_never_grants_authority(isolated_model, content, tool_call):
    services, model, factory = isolated_model
    model.ainvoke.return_value = AIMessage(
        content=content,
        tool_calls=[{"id": "unsafe", "name": "execute", "args": {}}] if tool_call else [],
        usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    )
    result = await reviews.TaskApprovalReviewer(services).generate(job())
    assert result == reviews.ApprovalReviewResult("requires_user", "review_invalid_response", 3, 2)
    factory.aclose.assert_awaited_once()


async def test_oversized_facts_are_not_truncated_into_an_automatic_allow(isolated_model):
    services, model, _factory = isolated_model
    result = await reviews.TaskApprovalReviewer(services).generate(job(proposal_json="界" * 11_000))
    assert result.reason_code == "review_context_too_large"
    model.ainvoke.assert_not_awaited()


def enqueue(services, review=None):
    with services.db.transaction() as conn:
        return services.project_task_review_repo.enqueue(conn, review or job())


def test_enqueue_is_one_attempt_and_conflicting_target_facts_fail(services):
    first = enqueue(services)
    again = enqueue(services, job(review_id="review_duplicate", created_at=2_000))
    assert first == again
    with pytest.raises(ValueError):
        enqueue(services, job(proposal_json='{"command":"a different command"}'))
    repo = services.project_task_review_repo
    claimed = repo.claim("worker_one", 1_000)
    assert claimed is not None and claimed.status == "reviewing"
    assert repo.claim("worker_two", 1_001) is None
    assert repo.read(claimed.review_id, 2) is None


async def test_sql_claim_fences_two_worker_connections(services):
    enqueue(services)
    other = SqlitePool(services.paths.root / "reviews.db")
    try:
        first, second = await asyncio.gather(
            asyncio.to_thread(services.project_task_review_repo.claim, "worker_one", 1_000),
            asyncio.to_thread(ProjectTaskReviewRepo(other).claim, "worker_two", 1_000),
        )
        assert sum(value is not None for value in (first, second)) == 1
    finally:
        other.close()


def test_unknown_dispatched_review_is_not_retried_and_late_usage_is_billed_once(services):
    enqueue(services)
    repo = services.project_task_review_repo
    claimed = repo.claim("dead_worker", 1_000)
    assert claimed is not None
    assert repo.claim("replacement", 91_001) is None
    recovered = repo.read(claimed.review_id, 1)
    assert (recovered.status, recovered.decision, recovered.reason_code) == (
        "completed",
        "requires_user",
        "review_unknown",
    )
    assert not repo.finish(claimed, "allow_once", "review_allow", 7, 2, 92_000)
    assert not repo.finish(claimed, "allow_once", "review_allow", 100, 100, 92_001)
    saved = repo.read(claimed.review_id, 1)
    assert saved.decision == "requires_user" and saved.reason_code == "review_unknown"
    assert services.usage_repo.total_tokens_for_user(1) == 9
    assert services.project_task_turn_repo.get("turn_one").input_tokens == 0
    assert repo.claim("replacement", 100_000) is None


def test_a_late_worker_cannot_win_by_finishing_before_the_recovery_scan(services):
    enqueue(services)
    repo = services.project_task_review_repo
    claimed = repo.claim("worker_one", 1_000)
    assert claimed is not None
    repo.finish(claimed, "allow_once", "review_allow", 7, 2, 91_001)
    saved = repo.read(claimed.review_id, 1)
    assert saved.decision == "requires_user" and saved.reason_code == "review_unknown"
    assert services.usage_repo.total_tokens_for_user(1) == 9


@pytest.mark.parametrize("cause", ["cancelled", "expired", "disabled", "turn_cancelled"])
def test_late_allow_cannot_restore_a_closed_review_but_known_usage_is_counted(services, cause):
    enqueue(services)
    repo = services.project_task_review_repo
    claimed = repo.claim("worker_one", 1_000)
    assert claimed is not None
    when = 1_002
    with services.db.transaction() as conn:
        if cause == "cancelled":
            repo.cancel(conn, "turn_one", 1_001)
        elif cause == "disabled":
            conn.execute("UPDATE users SET disabled=1 WHERE id=1")
        elif cause == "turn_cancelled":
            conn.execute(
                "UPDATE project_task_turns SET cancel_requested=1 WHERE turn_id='turn_one'"
            )
        else:
            when = claimed.expires_at
    repo.finish(claimed, "allow_once", "review_allow", 5, 2, when)
    saved = repo.read(claimed.review_id, 1)
    assert saved.decision != "allow_once"
    assert services.usage_repo.total_tokens_for_user(1) == 7
    repo.finish(claimed, "deny", "review_deny", 5, 2, when + 1)
    assert services.usage_repo.total_tokens_for_user(1) == 7


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("timeout", "review_timeout"),
        ("quota", "review_quota_exceeded"),
        ("provider", "review_unavailable"),
    ],
)
async def test_worker_fallbacks_are_durable_and_do_not_request_a_second_review(
    services, monkeypatch, failure, expected
):
    enqueue(services)
    repo = services.project_task_review_repo
    worker = reviews.TaskApprovalReviewWorker(services)
    claimed = repo.claim(worker.worker_id, 1_000)
    assert claimed is not None
    monkeypatch.setattr(reviews, "now_ms", lambda: 2_000)
    if failure == "timeout":

        async def generate(_job):
            await asyncio.Event().wait()

        worker.agent.generate = AsyncMock(side_effect=generate)
        monkeypatch.setattr(reviews, "_GENERATION_TIMEOUT", 0.01)
    else:
        exc = (
            OctopError(ErrorCode.TOKEN_QUOTA_EXCEEDED, "private quota details")
            if failure == "quota"
            else RuntimeError("private provider details")
        )
        worker.agent.generate = AsyncMock(side_effect=exc)
    await worker._execute(claimed)
    saved = repo.read(claimed.review_id, 1)
    assert saved.status == "completed" and saved.decision == "requires_user"
    assert saved.reason_code == expected
    assert repo.claim("second_worker", 3_000) is None
    worker.agent.generate.assert_awaited_once()


async def test_worker_finishes_allowed_result_with_its_own_usage(services, monkeypatch):
    enqueue(services)
    worker = reviews.TaskApprovalReviewWorker(services)
    worker.agent.generate = AsyncMock(
        return_value=reviews.ApprovalReviewResult("allow_once", "review_allow", 11, 4)
    )
    monkeypatch.setattr(reviews, "now_ms", lambda: 1_001)
    await worker.start()
    try:
        async with asyncio.timeout(3):
            while services.project_task_review_repo.read("review_one", 1).status != "completed":
                await asyncio.sleep(0.01)
    finally:
        await worker.close()
    assert services.usage_repo.total_tokens_for_user(1) == 15
    assert services.project_task_turn_repo.get("turn_one").output_tokens == 0
