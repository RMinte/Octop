"""Opt-in GLM ProviderStore → harness wire probe with a harmless tool.

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
args = parser.parse_args()
DB: Path = args.provider_db
PROVIDER: str = args.provider
MODEL: str = args.model


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


def capture(request: httpx.Request) -> None:
    body = json.loads(request.content)
    wire.append(
        {
            "path": request.url.path,
            "model": body.get("model"),
            "max_tokens": body.get("max_tokens", body.get("max_completion_tokens")),
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
    if len(wire) > 3:
        raise RuntimeError("request_limit_exceeded")
    if wire[-1]["model"] != MODEL:
        raise RuntimeError("wrong_model")
    if wire[-1]["max_tokens"] != 512:
        raise RuntimeError("missing_or_wrong_max_tokens")


sync_http = httpx.Client(timeout=20.0, event_hooks={"request": [capture]})


async def capture_async(request: httpx.Request) -> None:
    capture(request)


async_http = httpx.AsyncClient(timeout=20.0, event_hooks={"request": [capture_async]})
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
            tools=[synthetic_echo],
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
        try:
            request = ChatRequest(
                messages='请只调用 synthetic_echo 一次，参数 value="ping"，获得工具结果后用一句中文确认结果。不要使用其他工具。',
                configurable={"model_settings": {"max_tokens": 512}},
                recursion_limit=20,
            )
            result = await asyncio.wait_for(agent.call(request), timeout=50.0)
        except Exception as exc:
            failure = type(exc).__name__
        finally:
            await agent.aclose()
            await async_http.aclose()
            sync_http.close()
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
