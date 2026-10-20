"""Frozen server days and bounded keyset history across turns and reports."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from octop.config import OctopConfig
from octop.infra.agents.manager import AgentManager
from octop.infra.agents.resident_history import history_days, history_page
from octop.infra.agents.resident_identity import ensure_resident_identity
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path):
    paths = PathLayout(tmp_path / "octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    services.user_repo.create(username="owner", password_hash="x", role="user")
    services.user_repo.create(username="other", password_hash="x", role="user")
    manager = AgentManager(repos=services.repos, paths=paths)
    asyncio.run(ensure_resident_identity(services, manager, 1))
    asyncio.run(ensure_resident_identity(services, manager, 2))
    services.secret_repo.get_or_create("jwt", lambda: b"test-only-signature-key")
    yield services
    db.close()


def _turn(services, number: int, when: int, day: str = "", owner: int = 1):
    binding = services.resident_conversation_repo.get(owner)
    row, _ = services.resident_request_repo.create(
        owner,
        binding.thread_id,
        f"request-{number}",
        f"turn-{owner}-{number:03}",
        f"message {number} 50%_done",
        when,
        day_key=day,
        timezone="Asia/Shanghai" if day else "",
    )
    services.resident_request_repo.cancel(
        owner, binding.thread_id, f"cancel-{number}", row.turn_id, when + 1
    )
    return row


def _report(services, suffix: str, when: int, day: str):
    binding = services.resident_conversation_repo.get(1)
    with services.db.transaction() as conn:
        conn.execute(
            "INSERT INTO resident_delegations(delegation_id,owner_id,origin_agent_id,origin_thread_id,source_turn_id,task_id,initial_turn_id,request_id,fingerprint,timezone,created_at,updated_at) VALUES(?,1,?,?,?,?,?,?,?,'UTC',?,?)",
            (
                f"delegation-{suffix}",
                binding.agent_id,
                binding.thread_id,
                "source",
                "task",
                f"task-turn-{suffix}",
                f"delegate-request-{suffix}",
                "hash",
                when,
                when,
            ),
        )
        conn.execute(
            "INSERT INTO resident_task_reports(report_id,message_id,delegation_id,owner_id,thread_id,task_id,task_turn_id,status,text,day_key,timezone,captured_at,created_at,updated_at) VALUES(?,?,?,1,?,?,?,'completed',?,?,'UTC',?,?,?)",
            (
                suffix,
                f"message-{suffix}",
                f"delegation-{suffix}",
                binding.thread_id,
                "task",
                f"task-turn-{suffix}",
                "task result 50%_done",
                day,
                when,
                when,
                when,
            ),
        )


def test_day_backfill_is_frozen_once_even_when_completion_crosses_midnight(services):
    when = int(datetime(2026, 10, 20, 15, 59, tzinfo=UTC).timestamp() * 1000)
    row = _turn(services, 1, when)
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE resident_requests SET updated_at=?,finished_at=? WHERE turn_id=?",
            (when + 120_000, when + 120_000, row.turn_id),
        )
    repo = services.resident_history_repo
    repo.backfill_days("Asia/Shanghai")
    repo.backfill_days("America/Los_Angeles")
    binding = services.resident_conversation_repo.get(1)
    _, rows = repo.page(1, binding.thread_id, 10)
    assert rows[0].day_key == "2026-10-20"
    assert rows[0].timezone == "Asia/Shanghai"
    # Explicit metadata on a new turn is never recomputed by an upgrade sweep.
    _turn(services, 2, when + 120_000, "2026-10-21")
    repo.backfill_days("UTC")
    assert [row.day_key for row in repo.page(1, binding.thread_id, 10)[1]] == [
        "2026-10-21",
        "2026-10-20",
    ]


def test_seven_nonempty_days_date_location_and_scope_bound_cursors(services):
    binding = services.resident_conversation_repo.get(1)
    other = services.resident_conversation_repo.get(2)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    for number in range(10):
        when = start + timedelta(days=number * 2)
        _turn(services, number, int(when.timestamp() * 1000), when.date().isoformat())
    _turn(services, 10, int(start.timestamp() * 1000), "2026-10-01", owner=2)
    first = history_days(services, 1, binding.agent_id, binding.thread_id, 7, None, "")
    assert len(first.days) == 7 and first.next_cursor
    assert first.days[0].day_key == "2026-10-19"
    second = history_days(
        services, 1, binding.agent_id, binding.thread_id, 7, first.next_cursor, ""
    )
    assert len(second.days) == 3 and second.next_cursor is None
    assert {day.day_key for day in first.days}.isdisjoint(day.day_key for day in second.days)
    located = history_days(services, 1, binding.agent_id, binding.thread_id, 7, None, "2026-10-14")
    assert located.days[0].day_key == "2026-10-13"
    with pytest.raises(OctopError) as error:
        history_days(services, 2, other.agent_id, other.thread_id, 7, first.next_cursor, "")
    assert error.value.code.value == "PROJECT_SYNC_RESET"
    with pytest.raises(OctopError):
        history_days(
            services, 1, binding.agent_id, binding.thread_id, 7, first.next_cursor[:-1] + "!", ""
        )


def test_mixed_keyset_pages_never_skip_same_timestamp_and_search_is_literal(services):
    binding = services.resident_conversation_repo.get(1)
    for number in range(5):
        _turn(services, number, 1000, "2026-10-20")
    for suffix in ("a", "b", "c"):
        _report(services, suffix, 1000, "2026-10-20")
    repo = services.resident_history_repo
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        page = history_page(services, 1, binding.agent_id, binding.thread_id, 2, cursor, "zh")
        seen.extend(item.item_id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 8
    assert set(seen) == {f"turn:turn-1-{i:03}" for i in range(5)} | {
        "report:a",
        "report:b",
        "report:c",
    }
    assert (
        len(
            history_page(
                services, 1, binding.agent_id, binding.thread_id, 20, None, "zh", query="50%_done"
            ).items
        )
        == 8
    )
    assert not history_page(
        services, 1, binding.agent_id, binding.thread_id, 20, None, "zh", query="50X_done"
    ).items
    _, context, before, after = repo.location(1, binding.thread_id, "report:b", 1, 1)
    assert [item.item_id for item in context] == ["report:a", "report:b", "report:c"]
    assert not before and after
    assert repo.days(1, binding.thread_id, 7)[1] == [("2026-10-20", 8, ["Asia/Shanghai", "UTC"])]


def test_each_history_branch_applies_a_database_limit_before_merging(services, monkeypatch):
    binding = services.resident_conversation_repo.get(1)
    for number in range(200):
        _turn(services, number, 1000 + number, "2026-10-20")
    from octop.infra.db.repos.resident_history import ResidentHistoryRow

    original = ResidentHistoryRow.from_row
    loaded = []

    def count(row):
        loaded.append(row["item_id"])
        return original(row)

    monkeypatch.setattr(ResidentHistoryRow, "from_row", count)
    _, rows = services.resident_history_repo.page(1, binding.thread_id, 4)
    assert len(rows) == len(loaded) == 4
    assert rows[0].source_id == "turn-1-199"


def test_only_delivered_proactive_messages_join_days_search_pages_and_location(services):
    binding = services.resident_conversation_repo.get(1)
    _turn(services, 1, 1000, "2026-10-20")
    _report(services, "task", 1000, "2026-10-20")
    with services.db.transaction() as conn:
        for suffix, status in (
            ("a", "delivered"),
            ("b", "delivered"),
            ("c", "pending"),
            ("d", "discarded"),
        ):
            services.resident_notification_repo.enqueue(
                conn,
                notification_id=suffix,
                owner_id=1,
                agent_id=binding.agent_id,
                thread_id=binding.thread_id,
                followup_id="followup",
                authorization_version=1,
                source_key=f"notification-source-{suffix}",
                kind="note",
                text=f"Follow-up evidence {suffix} 50%_done",
                payload_json='{"purpose":"Follow-up"}',
                day_key="2026-10-20",
                timezone="UTC",
                now=1000,
            )
            conn.execute(
                "UPDATE resident_notifications SET status=? WHERE notification_id=?",
                (status, suffix),
            )
    seen = []
    cursor = None
    for _ in range(3):
        page = history_page(services, 1, binding.agent_id, binding.thread_id, 2, cursor, "en")
        seen.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(seen) == len({item.item_id for item in seen}) == 4
    notifications = [item for item in seen if item.kind == "notification"]
    assert {item.item_id for item in notifications} == {"notification:a", "notification:b"}
    assert notifications[0].notification.payload == {"purpose": "Follow-up"}
    assert notifications[0].notification.message_id.startswith("resident:notification:")
    search = history_page(
        services, 1, binding.agent_id, binding.thread_id, 20, None, "en", query="Follow-up evidence"
    )
    assert len(search.items) == 2
    days = history_days(services, 1, binding.agent_id, binding.thread_id, 7, None, "")
    assert days.days[0].item_count == 4
    _, context, before, after = services.resident_history_repo.location(
        1, binding.thread_id, "notification:b", 1, 1
    )
    assert [item.item_id for item in context] == ["notification:a", "notification:b", "report:task"]
    assert not before and after
    other = services.resident_conversation_repo.get(2)
    assert (
        services.resident_history_repo.location(2, other.thread_id, "notification:b", 1, 1)[1] == []
    )
