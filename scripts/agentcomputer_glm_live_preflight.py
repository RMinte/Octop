"""Opt-in GLM ProviderStore → harness wire probe with a tool or text stream.

Reads an existing Octop provider database in read-only mode. Never prints
credentials, request headers, prompts, or the model's full response.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import tool
from octop_harness.agent import HarnessAgent
from octop_harness.config import HarnessAgentConfig
from octop_harness.llm import factory as harness_factory
from octop_harness.request import ChatRequest

from octop.infra.agents.providers.store import ProviderStore

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--provider-db", required=True, type=Path)
parser.add_argument("--provider", required=True)
parser.add_argument("--model", required=True)
parser.add_argument("--mode", choices=("tool", "stream", "stream-zh"), default="tool")
args = parser.parse_args()
DB: Path = args.provider_db
PROVIDER: str = args.provider
MODEL: str = args.model
STREAM_MODE: bool = args.mode in ("stream", "stream-zh")
CHINESE_STREAM_MODE: bool = args.mode == "stream-zh"
STREAM_MARKER = "章鱼流式验证成功" if CHINESE_STREAM_MODE else "OCTOP_STREAM_OK"
MAX_REQUESTS = 1 if STREAM_MODE else 3
MAX_TOKENS = 256 if STREAM_MODE else 512
TOTAL_TIMEOUT = 45.0 if STREAM_MODE else 50.0


class ProviderRow:
    def __init__(self, values: tuple[Any, ...]):
        (
            self.name,
            self.kind,
            self.base_url,
            self.api_key,
            self.extra_json,
            self.models_json,
            self.enabled,
        ) = values

    def get_models(self) -> list[dict[str, Any]]:
        return json.loads(self.models_json or "[]")


class ReadOnlyRepo:
    def __init__(self, row: ProviderRow):
        self.row = row

    def list_all(self) -> list[ProviderRow]:
        return [self.row]


con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
try:
    values = con.execute(
        "SELECT name,kind,base_url,api_key,extra_json,models_json,enabled FROM providers WHERE name=?",
        (PROVIDER,),
    ).fetchone()
finally:
    con.close()
if values is None:
    raise SystemExit("provider_not_found")
row = ProviderRow(values)
if not row.enabled or not any(m.get("id") == MODEL and m.get("enabled") for m in row.get_models()):
    raise SystemExit("model_not_enabled")
configs = ProviderStore(ReadOnlyRepo(row)).build_harness_configs()
if len(configs) != 1:
    raise SystemExit("provider_unusable")

wire: list[dict[str, Any]] = []
called: list[str] = []
sse_responses: list[bool] = []


def capture(request: httpx.Request) -> None:
    body = json.loads(request.content)
    wire.append(
        {
            "path": request.url.path,
            "model": body.get("model"),
            "max_tokens": body.get("max_tokens", body.get("max_completion_tokens")),
            "stream": body.get("stream") is True,
            "roles": [m.get("role") for m in body.get("messages", [])],
            "tool_result_ids": [
                m.get("tool_call_id") for m in body.get("messages", []) if m.get("role") == "tool"
            ],
            "assistant_call_ids": [
                tc.get("id")
                for m in body.get("messages", [])
                if m.get("role") == "assistant"
                for tc in m.get("tool_calls", [])
            ],
        }
    )
    if len(wire) > MAX_REQUESTS:
        raise RuntimeError("request_limit_exceeded")
    if wire[-1]["model"] != MODEL:
        raise RuntimeError("wrong_model")
    if wire[-1]["max_tokens"] != MAX_TOKENS:
        raise RuntimeError("missing_or_wrong_max_tokens")
    if STREAM_MODE and not wire[-1]["stream"]:
        raise RuntimeError("stream_not_requested")


def capture_response(response: httpx.Response) -> None:
    sse_responses.append(
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        == "text/event-stream"
    )


async def capture_response_async(response: httpx.Response) -> None:
    capture_response(response)


sync_hooks: dict[str, list[Any]] = {"request": [capture]}
if STREAM_MODE:
    sync_hooks["response"] = [capture_response]
sync_http = httpx.Client(timeout=20.0, event_hooks=sync_hooks)


async def capture_async(request: httpx.Request) -> None:
    capture(request)


async_hooks: dict[str, list[Any]] = {"request": [capture_async]}
if STREAM_MODE:
    async_hooks["response"] = [capture_response_async]
async_http = httpx.AsyncClient(timeout=20.0, event_hooks=async_hooks)
original_get_class = harness_factory._get_reasoning_aware_chat_openai


def bounded_class(base_class: Any) -> Any:
    actual_class = original_get_class(base_class)

    def construct(**kwargs: Any) -> Any:
        return actual_class(
            http_client=sync_http,
            http_async_client=async_http,
            timeout=20.0,
            max_retries=0,
            **kwargs,
        )

    return construct


harness_factory._get_reasoning_aware_chat_openai = bounded_class


@tool
def synthetic_echo(value: str) -> str:
    """Return pong when the supplied value is ping; no other side effects."""
    called.append(value)
    return "pong" if value == "ping" else "unexpected_value"


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="octop-glm-preflight-") as tmp:
        config = HarnessAgentConfig(
            workspace_dir=Path(tmp),
            backend="filesystem",
            providers=configs,
            default_model=f"{PROVIDER}/{MODEL}",
            tools=[] if STREAM_MODE else [synthetic_echo],
            checkpointer=False,
            session_log_enabled=False,
            todos_enabled=False,
            ask_user_enabled=False,
            team_enabled=False,
            web_search_tools=False,
            pii_enabled=False,
            media_offload_enabled=False,
            model_retry_enabled=False,
            memory_enabled=False,
        )
        agent = await asyncio.to_thread(HarnessAgent, config)
        started = time.monotonic()
        result: dict[str, Any] | None = None
        failure: str | None = None
        stream_token_count = 0
        stream_snapshot_count = 0
        stream_usage_count = 0
        stream_has_chinese_token = False
        last_snapshot: dict[str, Any] | None = None
        stream_completed = False
        try:
            if STREAM_MODE:
                request = ChatRequest(
                    messages=f"请只输出 {STREAM_MARKER}，除此之外不要输出任何内容。",
                    configurable={"model_settings": {"max_tokens": MAX_TOKENS}},
                    recursion_limit=20,
                )

                async def consume_stream() -> None:
                    nonlocal stream_token_count, stream_snapshot_count, stream_usage_count
                    nonlocal stream_has_chinese_token
                    nonlocal last_snapshot, stream_completed
                    async for chunk in agent.stream(request, protocol="langgraph"):
                        if not isinstance(chunk, dict):
                            continue
                        if chunk.get("type") == "token":
                            token = str(chunk.get("content") or "")
                            if token.strip():
                                stream_token_count += 1
                                stream_has_chinese_token |= any(
                                    "\u4e00" <= char <= "\u9fff" for char in token
                                )
                        elif chunk.get("type") == "state_snapshot":
                            stream_snapshot_count += 1
                            if isinstance(chunk.get("data"), dict):
                                last_snapshot = chunk["data"]
                        elif chunk.get("type") == "usage":
                            stream_usage_count += 1
                    stream_completed = True

                await asyncio.wait_for(consume_stream(), timeout=TOTAL_TIMEOUT)
            else:
                request = ChatRequest(
                    messages='请只调用 synthetic_echo 一次，参数 value="ping"，获得工具结果后用一句中文确认结果。不要使用其他工具。',
                    configurable={"model_settings": {"max_tokens": MAX_TOKENS}},
                    recursion_limit=20,
                )
                result = await asyncio.wait_for(agent.call(request), timeout=TOTAL_TIMEOUT)
        except Exception as exc:
            failure = type(exc).__name__
        finally:
            await agent.aclose()
            await async_http.aclose()
            sync_http.close()
        if STREAM_MODE:
            messages = last_snapshot.get("messages", []) if last_snapshot else []
            final = messages[-1] if isinstance(messages, list) and messages else None
            final_contains_marker = STREAM_MARKER in str(getattr(final, "content", ""))
            passed = (
                failure is None
                and stream_completed
                and len(wire) == 1
                and wire[0]["stream"]
                and sse_responses == [True]
                and stream_token_count > 0
                and stream_snapshot_count > 0
                and getattr(final, "type", None) == "ai"
                and final_contains_marker
                and (not CHINESE_STREAM_MODE or stream_has_chinese_token)
                and not called
            )
            print(
                json.dumps(
                    {
                        "success": passed,
                        "mode": args.mode,
                        "provider": PROVIDER,
                        "model": MODEL,
                        "protocol_path": wire[0]["path"] if wire else None,
                        "request_count": len(wire),
                        "stream_requested": len(wire) == 1 and wire[0]["stream"],
                        "sse_response": sse_responses == [True],
                        "max_tokens_per_request": [w["max_tokens"] for w in wire],
                        "nonempty_token_events": stream_token_count,
                        "state_snapshot_events": stream_snapshot_count,
                        "stream_completed": stream_completed,
                        "final_contains_marker": final_contains_marker,
                        "chinese_token_seen": stream_has_chinese_token,
                        "usage_event_count": stream_usage_count,
                        "usage": "unknown",
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                        "failure_class": failure,
                    },
                    ensure_ascii=False,
                )
            )
            if not passed:
                raise SystemExit(1)
            return

        messages = result.get("messages", []) if result else []
        final = messages[-1] if messages else None
        usage = getattr(final, "usage_metadata", None)
        tool_ids_match = (
            len(wire) >= 2
            and bool(wire[-1]["tool_result_ids"])
            and wire[-1]["tool_result_ids"] == wire[-1]["assistant_call_ids"]
        )
        final_contains_pong = bool(final and "pong" in str(final.content).lower())
        passed = called == ["ping"] and tool_ids_match and final_contains_pong and failure is None
        print(
            json.dumps(
                {
                    "success": passed,
                    "provider": PROVIDER,
                    "model": MODEL,
                    "protocol_path": wire[0]["path"] if wire else None,
                    "request_count": len(wire),
                    "max_tokens_per_request": [w["max_tokens"] for w in wire],
                    "tool_called_once_with_ping": called == ["ping"],
                    "call_result_ids_match": tool_ids_match,
                    "final_contains_pong": final_contains_pong,
                    "final_message_usage": usage if isinstance(usage, dict) else None,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "failure_class": failure,
                },
                ensure_ascii=False,
            )
        )
        if not passed:
            raise SystemExit(1)


asyncio.run(main())
