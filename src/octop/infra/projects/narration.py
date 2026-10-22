"""Tool-free public narration that never holds up a task's execution."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage

from octop.i18n import tr
from octop.infra.agents.providers.box import OctopChatModelFactory as ChatModelFactory
from octop.infra.agents.providers.reasoning import reasoning_request_parameters
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.services import SharedServices
from octop.infra.users.resource_policy import assert_token_quota_available
from octop.infra.utils.llm_text import strip_thinking

if TYPE_CHECKING:
    from octop.infra.projects.content import TaskContent, TaskContentBlock

logger = logging.getLogger(__name__)
_TEXT_LIMIT = 240
_OUTPUT_BUDGET = 512
_RECENT_ACTION_LIMIT = 12
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_COUNT_KEYS = (
    "tests",
    "passed",
    "failed",
    "errors",
    "cancelled",
    "skipped",
    "todo",
    "xfailed",
    "xpassed",
)


def _output_text(output: Any) -> str | None:
    if isinstance(output, str):
        try:
            decoded = json.loads(output)
        except (ValueError, RecursionError):
            return output
        return _output_text(decoded) if isinstance(decoded, (dict, list)) else output
    if isinstance(output, dict):
        if output.get("truncated") is True:
            return None
        for field in ("output", "stdout"):
            if field in output:
                return _output_text(output[field])
    if isinstance(output, list):
        parts = [
            block["text"]
            for block in output
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return "\n".join(parts) if parts else None
    return None


def extract_test_counts(output: Any) -> dict[str, int] | None:
    """Read one explicit, internally consistent runner summary before excerpting."""
    text = _output_text(output)
    if text is None or re.search(r"\[Output[^\]\n]*truncated|saved file is incomplete", text, re.I):
        return None
    text = _ANSI.sub("", text)
    node: dict[str, int] = {}
    for name, count in re.findall(
        r"^(?:ℹ|#)\s+(tests|pass|fail|cancelled|skipped|todo)\s+(\d+)\s*$", text, re.M
    ):
        if name in node:
            return None
        node[name] = int(count)
    if {"tests", "pass", "fail"} <= node.keys():
        if node["tests"] != sum(value for key, value in node.items() if key != "tests"):
            return None
        return {
            ({"pass": "passed", "fail": "failed"}.get(key, key)): value
            for key, value in node.items()
        }
    if node:
        return None

    summaries = re.findall(
        r"^\s*(?:=+\s*)?((?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)(?:,\s*)?)+)\s+in\s+\d+(?:\.\d+)?s(?:\s+\([^\n]*\))?\s*(?:=+)?\s*$",
        text,
        re.M,
    )
    if summaries:
        if len(summaries) != 1:
            return None
        counts: dict[str, int] = {}
        for count, name in re.findall(r"(\d+)\s+([a-z]+)", summaries[0]):
            name = "errors" if name == "error" else name
            if name in counts:
                return None
            counts[name] = int(count)
        counts = {key: value for key, value in counts.items() if key in _COUNT_KEYS}
        if not counts:
            return None
        if not counts.get("errors"):
            counts["tests"] = sum(counts.values())
        return counts

    plans = re.findall(r"^1\.\.(\d+)\s*$", text, re.M)
    cases = re.findall(r"^(not ok|ok)\s+(\d+)\b([^\n]*)$", text, re.M)
    if len(plans) != 1 or "Bail out!" in text:
        return None
    total = int(plans[0])
    if total != len(cases) or [int(case[1]) for case in cases] != list(range(1, total + 1)):
        return None
    counts = {"tests": total, "passed": 0, "failed": 0, "skipped": 0, "todo": 0}
    for status, _number, detail in cases:
        directive = re.search(r"#\s*(SKIP|TODO)\b", detail, re.I)
        key = (
            {"skip": "skipped", "todo": "todo"}[directive[1].lower()]
            if directive
            else "passed"
            if status == "ok"
            else "failed"
        )
        counts[key] += 1
    return counts


def _result_excerpt(output: Any, limit: int) -> tuple[str, bool]:
    text = json.dumps(output, ensure_ascii=False)
    if len(text) <= limit:
        return text, False
    return text[: limit // 2] + "\n[partial result]\n" + text[-limit // 2 :], True


def progress_context(content: TaskContent) -> dict[str, Any]:
    """Bounded facts from this turn; never reuse generated briefs as evidence."""
    plan = content.plan
    context: dict[str, Any] = {
        "scope": "current_turn_only; omitted or unknown is not evidence of not started"
    }
    if plan is not None:
        selected = list(range(len(plan.steps)))
        if len(selected) > 12:
            ended = [
                index
                for index, step in enumerate(plan.steps)
                if step.status in {"completed", "blocked", "skipped"}
            ]
            active = next(
                (
                    index
                    for index, step in enumerate(plan.steps)
                    if step.step_id == plan.active_step_id
                ),
                None,
            )
            selected = sorted(
                {
                    *ended[-(11 if active is not None else 12) :],
                    *([active] if active is not None else []),
                }
            )
        context["plan"] = {
            "plan_id": plan.plan_id,
            "title": plan.title,
            "revision": plan.revision,
            "phase": "blocked"
            if any(step.status == "blocked" for step in plan.steps)
            else "executing"
            if plan.active_step_id
            else "completed"
            if all(step.status in {"completed", "skipped"} for step in plan.steps)
            else "unknown",
            "active_step_id": plan.active_step_id,
            "steps_omitted": len(plan.steps) - len(selected),
            "steps": [
                {
                    "step_id": step.step_id,
                    "content": step.content,
                    "status": step.status,
                    "result": step.result[:600],
                    "result_truncated": len(step.result) > 600,
                }
                for index in selected
                for step in [plan.steps[index]]
            ],
        }
    actions = [
        block for block in content.blocks if block.type == "tool" and block.started_at is not None
    ]
    context["executed_action_count"] = len(actions)
    context["actions_omitted"] = max(0, len(actions) - _RECENT_ACTION_LIMIT)
    recent = []
    for block in actions[-_RECENT_ACTION_LIMIT:]:
        fact: dict[str, Any] = {
            "tool_call_id": block.tool_call_id,
            "action": block.tool_label or block.tool_name,
            "status": block.status,
            "step_id": block.step_id,
        }
        arguments = block.arguments or {}
        for field in ("path", "file_path", "name"):
            if isinstance(arguments.get(field), str):
                fact["target"] = arguments[field][:256]
                break
        if block.output is not None:
            fact["result"], fact["result_truncated"] = _result_excerpt(block.output, 300)
        recent.append(fact)
    context["recent_actions"] = recent
    return context


def tool_progress_facts(
    content: TaskContent, block: TaskContentBlock, *, finished: bool
) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "stage": "tool_end" if finished else "tool_start",
        "action": block.tool_label or block.tool_name,
        "status": block.status,
        "context": progress_context(content),
    }
    for field in ("path", "file_path", "name"):
        if isinstance((block.arguments or {}).get(field), str):
            facts["target"] = (block.arguments or {})[field][:256]
            break
    if finished:
        facts["result"], facts["result_truncated"] = _result_excerpt(block.output, 1200)
        if block.status == "completed" and block.tool_name in {"write_file", "edit_file"}:
            receipt = _output_text(block.output) or ""
            match = (
                re.fullmatch(r"Updated file (.+)", receipt)
                if block.tool_name == "write_file"
                else re.fullmatch(
                    r"Successfully replaced [1-9]\d* instance\(s\) of the string in '(.+)'", receipt
                )
            )
            if match:
                facts["file_result"] = {
                    "operation": "saved" if block.tool_name == "write_file" else "updated",
                    "target": match[1],
                }
        if (
            block.tool_name in {"execute", "client__exec_command", "client__write_stdin"}
            and (counts := extract_test_counts(block.output)) is not None
        ):
            facts["test_counts"] = counts
    return facts


def _test_summary(counts: dict[str, Any], locale: str) -> str | None:
    values = [
        tr(
            "projects.progress_test_count",
            locale,
            label=tr(f"projects.progress_test_count_labels.{key}", locale),
            count=counts[key],
        )
        for key in _COUNT_KEYS
        if key in counts
    ]
    return _plain_text(tr("projects.progress_test_summary", locale, counts=", ".join(values)))


def _unsupported_test_counts(text: str) -> bool:
    number = r"(?:\d+|[零一二三四五六七八九十百千万两]+)"
    return any(
        re.search(pattern, text, re.I)
        for pattern in (
            r"\b\d+\s+(?:tests?\s+(?:(?:have|has)\s+)?)?(?:passed|failed|passing|failing|skipped|cancelled|xfailed|xpassed)\b",
            r"\b(?:tests?|passed|failed|skipped)\s*[:=]\s*\d+\b",
            r"\b(?:passed|failed|passing|failing)\s+\d+(?:\s+tests?)?\b",
            rf"(?:通过|失败|跳过|取消)\s*[:：]\s*{number}",
            rf"(?:通过|失败|跳过|取消)(?:了)?\s*{number}\s*(?:个|项|条)?\s*(?:测试|用例)",
            rf"(?:通过|失败|跳过|取消)\s*{number}\s*(?:项|个|条)(?!文件|脚本)",
            rf"{number}\s*(?:个|项|条)?\s*(?:测试|用例)\s*(?:全部|均|都)?(?:已)?(?:通过|失败|跳过|取消)",
            rf"(?:共|总计|合计)\s*{number}\s*(?:个|项|条)?\s*(?:测试|用例)(?!文件|脚本)",
            rf"(?<![a-z0-9/]){number}\s*(?:项|个|条)?\s*(?:通过|失败)(?!校验|验证)",
            rf"{number}\s*/\s*{number}\s*(?:全部通过|passed|passing)",
            r"\ball\s+tests?\s+(?:passed|pass|succeeded)\b",
            r"(?:测试|用例)\s*(?:全部通过|均通过|全绿)",
        )
    )


def _unsupported_global_absence(text: str) -> bool:
    # A partial turn snapshot cannot establish that an entire work phase never began.
    return any(
        re.search(pattern, text, re.I)
        for pattern in (
            r"(?:尚未|还未|还没(?:有)?|没有)(?:开始|进行|开展)?(?:任何)?(?:界面|前端|UI|编码|开发|实现|测试)",
            r"(?:界面|前端|UI|编码|开发|实现|测试)[^。！？.;!?]{0,12}(?:尚未|还未|还没(?:有)?|未)(?:开始|进行)",
            r"\b(?:UI|frontend|coding|implementation|development|tests?|testing)\b[^.!?]{0,24}\b(?:not|never|yet to)\b[^.!?]{0,12}\b(?:start|started|begun|done|implemented|run)\b",
            r"\bno\s+(?:coding|development|implementation|testing)\s+(?:has\s+)?(?:started|begun|been\s+done)\b",
        )
    )


def _unsupported_opening_state(text: str) -> bool:
    """An opening can describe intent, never a durable claim about absent results."""
    return any(
        re.search(pattern, text, re.I)
        for pattern in (
            r"(?:没有|尚无|暂无|未收到|缺少|缺乏)[^。！？.;!?]{0,16}(?:回执|工具结果|执行结果|证据)",
            r"(?:尚未|还未|还没(?:有)?|没有|未)(?:开始|进行|执行|开展)?[^。！？.;!?]{0,20}(?:创建|读取|写入|文件|操作|工具)",
            r"(?:无法|不能|尚不能|未能)[^。！？.;!?]{0,8}(?:确认|验证|确定)[^。！？.;!?]{0,12}(?:结果|事实|任何|操作)",
            r"\b(?:no|without|lacking|haven['’]t received)\b[^.!?]{0,32}\b(?:receipts?|tool results?|execution results?|evidence)\b",
            r"\b(?:not yet|haven['’]t|hasn['’]t|not)\b[^.!?]{0,20}\b(?:started|created|read|written|executed)\b",
            r"\b(?:cannot|can't|unable to)\s+(?:confirm|verify|determine)\b[^.!?]{0,25}\b(?:results?|outcomes?|anything)\b",
        )
    )


@dataclass(frozen=True)
class ProgressSnapshot:
    key: str
    facts: dict[str, Any]
    plan_id: str | None = None
    step_id: str | None = None
    tool_call_id: str | None = None
    source_digest: str | None = None


def _plain_text(content: Any) -> str | None:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            block
            if isinstance(block, str)
            else block["text"]
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            else ""
            for block in content
        )
    else:
        return None
    text = strip_thinking(text)
    if not text or len(text) > _TEXT_LIMIT or "```" in text:
        return None
    return " ".join(text.split()) or None


def _reasoning_parameters(capability: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if capability is None:
        return {}
    capability = dict(capability)
    capability["default_effort"] = None
    if capability.get("toggle", True):
        return reasoning_request_parameters(capability, mode="disabled", effort=None)
    efforts = capability.get("efforts") or []
    order = ["minimal", "low", "medium", "high", "xhigh", "max"]
    effort = next((item for item in order if item in efforts), None)
    if capability.get("effort_type") == "token_budget":
        budgets = [str(item) for item in efforts if str(item).isdigit()]
        effort = min(budgets, key=int) if budgets else None
    return reasoning_request_parameters(capability, mode="enabled", effort=effort)


class TaskProgressAgent:
    def __init__(
        self,
        services: SharedServices,
        owner_id: int,
        model_ref: str,
        locale: str,
        *,
        record_usage: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> None:
        self.services = services
        self.owner_id = owner_id
        self.model_ref = model_ref
        self.locale = locale
        self.record_usage = record_usage

    async def generate(self, snapshot: ProgressSnapshot) -> str | None:
        if isinstance(snapshot.facts.get("test_counts"), dict):
            # Numeric runner evidence is rendered by the host, never paraphrased.
            return _test_summary(snapshot.facts["test_counts"], self.locale)
        if isinstance(snapshot.facts.get("file_result"), dict):
            receipt = snapshot.facts["file_result"]
            return _plain_text(
                tr(
                    f"projects.progress_file_{receipt['operation']}",
                    self.locale,
                    target=receipt["target"],
                )
            )
        services = self.services
        await asyncio.to_thread(
            assert_token_quota_available,
            services.user_policy_repo,
            services.usage_repo,
            self.owner_id,
        )
        user = await asyncio.to_thread(services.user_repo.get, self.owner_id)
        if user is None or user.disabled:
            return None
        store = ProviderStore(services.provider_repo)
        if not await asyncio.to_thread(store.is_model_ref_usable, self.model_ref):
            return None
        capability = await asyncio.to_thread(store.get_model_reasoning_capability, self.model_ref)
        providers = await asyncio.to_thread(store.build_harness_configs)
        factory = ChatModelFactory(providers)
        try:
            model = await asyncio.to_thread(factory.get_chat_model, self.model_ref)
            overrides = _reasoning_parameters(capability)
            updates = {"max_retries": 0, **overrides.get("model_fields", {})}
            if extra := overrides.get("extra_body"):
                updates["extra_body"] = {**(getattr(model, "extra_body", None) or {}), **extra}
            configured = model.model_copy(update=updates)
            limit = getattr(model, "_harness_max_output_tokens", 0) or _OUTPUT_BUDGET
            result = await configured.ainvoke(
                [
                    SystemMessage(tr("projects.progress_narration_prompt", self.locale)),
                    HumanMessage(json.dumps(snapshot.facts, ensure_ascii=False)),
                ],
                max_tokens=min(limit, _OUTPUT_BUDGET),
            )
            usage = result.usage_metadata
            if usage is not None and self.record_usage is not None:
                await self.record_usage(usage["input_tokens"], usage["output_tokens"])
            if result.tool_calls or result.invalid_tool_calls:
                return None
            text = _plain_text(result.content)
            return (
                text
                if text is not None
                and not _unsupported_test_counts(text)
                and not _unsupported_global_absence(text)
                and not (
                    snapshot.facts.get("stage") == "received" and _unsupported_opening_state(text)
                )
                else None
            )
        finally:
            await factory.aclose()


class TaskProgressNarrator:
    """One active generation and one replaceable pending snapshot per turn."""

    def __init__(
        self,
        generate: Callable[[ProgressSnapshot], Awaitable[str | None]],
        publish: Callable[[ProgressSnapshot, str], Awaitable[None]],
        *,
        timeout: float = 8.0,
    ) -> None:
        self.generate = generate
        self.publish = publish
        self.timeout = timeout
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._pending: tuple[int, ProgressSnapshot] | None = None
        self._seen: set[str] = set()
        self._sequence = 0
        self._latest_key: str | None = None
        self._closed = False

    def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._loop())
            self._task.add_done_callback(self._finished)

    def submit(self, snapshot: ProgressSnapshot) -> None:
        if self._closed or snapshot.key in self._seen:
            return
        self._seen.add(snapshot.key)
        self._sequence += 1
        self._latest_key = snapshot.key
        self._pending = self._sequence, deepcopy(snapshot)
        self._wake.set()

    def is_current(self, snapshot: ProgressSnapshot) -> bool:
        return not self._closed and snapshot.key == self._latest_key

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._sequence += 1
        self._pending = None
        if self._task is not None:
            self._task.cancel()
            # Provider cleanup must not hold up the main turn's final output.
            # Keep the task referenced until its callback consumes completion.
            await asyncio.sleep(0)

    def _finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        if self._task is task:
            self._task = None

    async def _loop(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            pending, self._pending = self._pending, None
            if pending is None:
                continue
            sequence, snapshot = pending
            try:
                text = await asyncio.wait_for(self.generate(snapshot), self.timeout)
                if text and not self._closed and sequence == self._sequence:
                    await self.publish(snapshot, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Facts and provider exception messages can contain private context.
                logger.debug("Task progress narration skipped (%s)", type(exc).__name__)
