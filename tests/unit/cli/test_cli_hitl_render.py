"""CLI HITL pause rendering and MessageEvent conversion."""

from __future__ import annotations

from octop_gateway.models import MessageEvent, MessageEventType, TextContent

from octop.cli.repl.render import format_cli_hitl_pause
from octop.infra.gateway.cli.events import message_event_to_cli_chunks


def test_format_cli_hitl_pause_includes_approve_hint() -> None:
    card = format_cli_hitl_pause(
        {
            "type": "hitl_required",
            "pending_id": "ab12",
            "request": {
                "action_requests": [{"name": "execute", "args": {"command": "rm -rf /tmp/x"}}]
            },
        }
    )
    assert "/approve" in card
    assert "ab12" in card
    en = format_cli_hitl_pause(
        {
            "type": "hitl_required",
            "pending_id": "ab12",
            "request": {
                "action_requests": [{"name": "execute", "args": {"command": "rm -rf /tmp/x"}}]
            },
        },
        locale="en",
    )
    assert "Tool approval required" in en


def test_format_cli_hitl_pause_ask_does_not_use_approve() -> None:
    card = format_cli_hitl_pause(
        {
            "type": "hitl_required",
            "pending_id": "q1",
            "request": {
                "action_requests": [
                    {
                        "name": "ask_user_question",
                        "args": {"questions": [{"question": "用哪个库?"}]},
                    }
                ]
            },
        }
    )
    assert "用哪个库" in card
    assert "/approve" not in card


def test_message_event_to_cli_chunks() -> None:
    text = MessageEvent(
        type=MessageEventType.MESSAGE,
        content=[TextContent(text="已批准，继续执行…")],
    )
    assert message_event_to_cli_chunks(text) == [
        {"type": "token", "content": "已批准，继续执行…\n"}
    ]
    err = MessageEvent(
        type=MessageEventType.ERROR,
        content=[TextContent(text="boom")],
    )
    assert message_event_to_cli_chunks(err) == [{"type": "error", "message": "boom"}]
    assert message_event_to_cli_chunks(MessageEvent(type=MessageEventType.TYPING)) == []


def test_task_question_card_uses_exact_answer_command() -> None:
    card = format_cli_hitl_pause(
        {
            "interaction": {
                "interaction_id": "interaction-full-id",
                "kind": "question",
                "payload": {"question": "Which library?", "options": [{"label": "SQLite"}]},
            }
        },
        locale="en",
    )
    assert "Which library?" in card and "SQLite" in card
    assert "/answer interaction-full-id <answer>" in card
    assert "/approve" not in card


def test_task_approval_card_shows_exact_action_and_interaction() -> None:
    card = format_cli_hitl_pause(
        {
            "interaction": {
                "interaction_id": "approval-full-id",
                "kind": "approval",
                "payload": {
                    "tool_name": "execute",
                    "reason": "Verify the project",
                    "arguments": {"command": "python -m pytest tests/unit/selected"},
                },
            }
        },
        locale="en",
    )
    assert "python -m pytest tests/unit/selected" in card
    assert "/approve approval-full-id" in card and "/deny approval-full-id" in card
