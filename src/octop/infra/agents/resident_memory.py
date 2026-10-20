"""Explicit user-managed facts in the existing private Agent memory store."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import Field, field_validator

from octop.infra.agents.memory.backend import open_memory_kwargs
from octop.infra.agents.resident_conversation import ResidentModel
from octop.infra.agents.resident_history import require_resident_binding
from octop.infra.agents.workspace.dir import resolve_workspace_host_path
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError

if TYPE_CHECKING:
    from octop_harness.memory.llm_client import HarnessAgentLLMClient
    from octop_memory.core import Memory
    from octop_memory.types import AtomCard, Candidate

    from octop.infra.agents.manager import AgentManager

MemoryKind = Literal["Fact", "Decision", "Task", "Preference"]
MemoryCandidateStatus = Literal[
    "pending", "promoted", "rejected", "needs_review", "conflict", "extractor_failed"
]


class ResidentMemoryWrite(ResidentModel):
    assertion: str = Field(
        min_length=1,
        max_length=2048,
        description="A fact the user explicitly chooses to save; not an automatic conversation capture.",
    )
    kind: MemoryKind = "Fact"
    source_turn_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Optional existing turn in this exact private conversation. Only its verified anchor is saved, not its full content.",
    )

    @field_validator("assertion")
    @classmethod
    def nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("assertion must contain text")
        return value


class ResidentMemoryReplace(ResidentModel):
    assertion: str = Field(min_length=1, max_length=2048)

    @field_validator("assertion")
    @classmethod
    def nonempty(cls, value: str) -> str:
        return ResidentMemoryWrite.nonempty(value)


class ResidentMemoryDeprecate(ResidentModel):
    reason: str = Field(default="", max_length=1024)


class ResidentMemoryItem(ResidentModel):
    atom_id: str
    assertion: str
    kind: str
    created_at: datetime
    deprecated_at: datetime | None
    superseded_by: str | None
    source_event_ids: list[str]
    quote_event_id: str


class ResidentMemoryPage(ResidentModel):
    items: list[ResidentMemoryItem]
    has_more: bool


class ResidentMemoryCandidate(ResidentModel):
    candidate_id: str
    assertion: str
    kind: str
    status: MemoryCandidateStatus
    quote: str
    source_event_ids: list[str]
    quote_event_id: str
    created_at: datetime
    decided_at: datetime | None
    decided_by: str | None


class ResidentMemoryCandidatePage(ResidentModel):
    items: list[ResidentMemoryCandidate]
    has_more: bool


class ResidentMemorySource(ResidentModel):
    event_id: str
    event_type: str
    content: str
    timestamp: datetime
    source_turn_id: str | None = None
    source_message_id: str | None = None
    history_item_id: str | None = None


class ResidentMemorySources(ResidentModel):
    items: list[ResidentMemorySource]


class ResidentMemoryReject(ResidentModel):
    reason: str = Field(default="", max_length=1024)


class ResidentMemoryPolicy(ResidentModel):
    capture_enabled: bool
    revision: int
    mode: Literal["disabled", "raw_capture_only"]


class ResidentMemoryPolicyUpdate(ResidentModel):
    capture_enabled: bool
    expected_revision: int = Field(ge=1)


class ResidentMemoryExtraction(ResidentModel):
    status: Literal["completed", "failed", "empty"]
    candidates: int = 0
    events_extracted: int = 0
    pending_events: int = 0
    has_earlier: bool = False
    truncated_events: int = 0
    scope: Literal["recent_user_messages"] = "recent_user_messages"
    error_code: str | None = None


def resident_memory_policy(
    services: SharedServices, owner_id: int, agent_id: str, thread_id: str
) -> ResidentMemoryPolicy:
    require_resident_binding(services, owner_id, agent_id, thread_id)
    policy = services.resident_memory_policy_repo.get(owner_id, thread_id)
    assert policy is not None
    return ResidentMemoryPolicy(
        capture_enabled=policy.capture_enabled,
        revision=policy.revision,
        mode="raw_capture_only" if policy.capture_enabled else "disabled",
    )


def update_resident_memory_policy(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    body: ResidentMemoryPolicyUpdate,
) -> ResidentMemoryPolicy:
    with _memory_control(
        services, owner_id, agent_id, thread_id, allow_archived=not body.capture_enabled
    ) as conn:
        policy = services.resident_memory_policy_repo.update_in_transaction(
            conn, owner_id, thread_id, body.expected_revision, body.capture_enabled
        )
    if policy is None:
        raise OctopError.localized(ErrorCode.RESIDENT_MEMORY_CONFLICT)
    return ResidentMemoryPolicy(
        capture_enabled=policy.capture_enabled,
        revision=policy.revision,
        mode="raw_capture_only" if policy.capture_enabled else "disabled",
    )


@contextmanager
def _memory_control(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    *,
    allow_archived: bool = False,
) -> Iterator[Any]:
    # The same short-lived owner lock guards archive and memory decisions.
    # It avoids concurrent candidate approvals upgrading SQLite read locks.
    with services.db.transaction() as conn:
        if not services.user_repo.lock_identity_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        require_resident_binding(services, owner_id, agent_id, thread_id)
        profile = services.resident_profile_repo.get_for_agent(owner_id, agent_id)
        if profile is not None and profile.archived and not allow_archived:
            raise OctopError.localized(ErrorCode.RESIDENT_PROFILE_ARCHIVED)
        yield conn


@contextmanager
def _memory_write(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    *,
    allow_archived: bool = False,
) -> Iterator[Memory]:
    with (
        _memory_control(services, owner_id, agent_id, thread_id, allow_archived=allow_archived),
        _memory(services, owner_id, agent_id, thread_id) as memory,
    ):
        yield memory


@contextmanager
def _memory(
    services: SharedServices, owner_id: int, agent_id: str, thread_id: str
) -> Iterator[Memory]:
    from octop_memory.core import Memory

    require_resident_binding(services, owner_id, agent_id, thread_id)
    row = services.agent_repo.get(agent_id)
    assert row is not None
    cfg = json.loads(row.config_json or "{}")
    workspace = resolve_workspace_host_path(str(cfg["workspace_dir"]), cfg)
    namespace, backend, config = open_memory_kwargs(
        agent_id=agent_id, cfg=cfg, octop_config=services.config, workspace_dir=workspace
    )
    memory = Memory(namespace=namespace, backend=backend, backend_config=config)
    try:
        yield memory
    finally:
        close = getattr(memory.backend, "close", None)
        if callable(close):
            close()


def _view(memory: Memory, atom: AtomCard) -> ResidentMemoryItem:
    candidate = memory.get_candidate(atom.candidate_id) if atom.candidate_id else None
    return ResidentMemoryItem(
        atom_id=atom.id,
        assertion=atom.assertion,
        kind=candidate.candidate_type if candidate else "Fact",
        created_at=atom.created_at,
        deprecated_at=atom.deprecated_at,
        superseded_by=atom.superseded_by,
        source_event_ids=atom.raw_event_ids,
        quote_event_id=atom.quote_event_id,
    )


def list_resident_memories(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    limit: int,
    include_deprecated: bool,
) -> ResidentMemoryPage:
    with _memory(services, owner_id, agent_id, thread_id) as memory:
        atoms = memory.list_atoms(limit=limit + 1, include_deprecated=include_deprecated)
        return ResidentMemoryPage(
            items=[_view(memory, atom) for atom in atoms[:limit]], has_more=len(atoms) > limit
        )


def create_resident_memory(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    body: ResidentMemoryWrite,
) -> ResidentMemoryItem:
    note = ""
    if body.source_turn_id is not None:
        source = services.resident_request_repo.get_by_turn(body.source_turn_id, owner_id)
        if source is None or source.thread_id != thread_id:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        note = json.dumps(
            {
                "resident_source": {
                    "thread_id": thread_id,
                    "turn_id": source.turn_id,
                    "message_id": f"resident:{source.turn_id}:user",
                }
            }
        )
    with _memory_write(services, owner_id, agent_id, thread_id) as memory:
        try:
            user = services.user_repo.get(owner_id)
            assert user is not None
            atom, _, _ = memory.create_atom(
                body.assertion,
                entity_name=user.username,
                entity_type="User",
                kind=body.kind,
                actor="user",
                note=note,
            )
        except ValueError as exc:
            raise OctopError.localized(ErrorCode.RESIDENT_MEMORY_CONFLICT) from exc
        return _view(memory, atom)


def replace_resident_memory(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    atom_id: str,
    body: ResidentMemoryReplace,
) -> ResidentMemoryItem:
    with _memory_write(services, owner_id, agent_id, thread_id, allow_archived=True) as memory:
        try:
            atom = memory.replace_atom(atom_id, assertion=body.assertion, actor="user")
        except ValueError as exc:
            raise OctopError.localized(ErrorCode.RESIDENT_MEMORY_CONFLICT) from exc
        if atom is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        return _view(memory, atom)


def deprecate_resident_memory(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    atom_id: str,
    body: ResidentMemoryDeprecate,
) -> ResidentMemoryItem:
    with _memory_write(services, owner_id, agent_id, thread_id, allow_archived=True) as memory:
        atom = memory.get_atom(atom_id)
        if atom is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        memory.deprecate_atom(atom_id, actor="user", note=body.reason)
        current = memory.get_atom(atom_id)
        assert current is not None
        return _view(memory, current)


def _candidate_view(candidate: Candidate) -> ResidentMemoryCandidate:
    return ResidentMemoryCandidate(
        candidate_id=candidate.id,
        assertion=candidate.assertion,
        kind=candidate.candidate_type,
        status=candidate.status,
        quote=candidate.verbatim_quote,
        source_event_ids=candidate.raw_event_ids,
        quote_event_id=candidate.quote_event_id,
        created_at=candidate.created_at,
        decided_at=candidate.decided_at,
        decided_by=candidate.decided_by,
    )


def resident_memory_candidates(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    limit: int,
    status: MemoryCandidateStatus | None,
) -> ResidentMemoryCandidatePage:
    with _memory(services, owner_id, agent_id, thread_id) as memory:
        candidates = memory.list_candidates(status=status, limit=limit + 1)
        return ResidentMemoryCandidatePage(
            items=[_candidate_view(candidate) for candidate in candidates[:limit]],
            has_more=len(candidates) > limit,
        )


def resident_memory_candidate(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    candidate_id: str,
) -> ResidentMemoryCandidate:
    with _memory(services, owner_id, agent_id, thread_id) as memory:
        candidate = memory.get_candidate(candidate_id)
        if candidate is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        return _candidate_view(candidate)


def decide_resident_memory_candidate(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    candidate_id: str,
    *,
    approve: bool,
    reason: str = "",
) -> ResidentMemoryCandidate:
    from octop_memory.pipeline.promotion import PromotionWorker
    from octop_memory.types import JournalEntry

    with (
        _memory_write(
            services, owner_id, agent_id, thread_id, allow_archived=not approve
        ) as memory,
        memory.backend.transaction(),
    ):
        candidate = memory.get_candidate(candidate_id)
        if candidate is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        target_status = "promoted" if approve else "rejected"
        if candidate.status == target_status:
            return _candidate_view(candidate)
        if candidate.status in {"promoted", "rejected", "extractor_failed"}:
            raise OctopError.localized(ErrorCode.RESIDENT_MEMORY_CONFLICT)
        if approve:
            try:
                # A user decision reuses canonical promotion, with no LLM hook.
                PromotionWorker(memory).approve(candidate)
            except ValueError as exc:
                raise OctopError.localized(ErrorCode.RESIDENT_MEMORY_CONFLICT) from exc
        else:
            now = datetime.now(UTC)
            memory.update_candidate_status(
                candidate.id, status="rejected", decided_by="user", decided_at=now
            )
            memory.append_journal(
                JournalEntry(
                    id=str(uuid4()),
                    timestamp=now,
                    action="reject",
                    actor="user",
                    target_candidate_id=candidate.id,
                    note=reason,
                )
            )
        current = memory.get_candidate(candidate_id)
        assert current is not None
        return _candidate_view(current)


def resident_memory_sources(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    item_id: str,
    *,
    candidate: bool = False,
) -> ResidentMemorySources:
    with _memory(services, owner_id, agent_id, thread_id) as memory:
        subject = memory.get_candidate(item_id) if candidate else memory.get_atom(item_id)
        if subject is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        sources: list[ResidentMemorySource] = []
        for raw in memory.get_raw_events_by_ids(subject.raw_event_ids[:50]):
            if raw.thread_id is not None and raw.thread_id != thread_id:
                continue
            source = ResidentMemorySource(
                event_id=raw.id,
                event_type=raw.event_type,
                content=raw.content[:8192],
                timestamp=raw.timestamp,
            )
            note = raw.payload.get("note")
            if isinstance(note, str):
                try:
                    decoded = json.loads(note)
                except (TypeError, ValueError):
                    decoded = None
                anchor = decoded.get("resident_source") if isinstance(decoded, dict) else None
                if isinstance(anchor, dict) and anchor.get("thread_id") == thread_id:
                    turn_id = anchor.get("turn_id")
                    turn = (
                        services.resident_request_repo.get_by_turn(turn_id, owner_id)
                        if isinstance(turn_id, str)
                        else None
                    )
                    if turn is not None and turn.thread_id == thread_id:
                        source.source_turn_id = turn.turn_id
                        source.source_message_id = f"resident:{turn.turn_id}:user"
                        source.history_item_id = f"turn:{turn.turn_id}"
            sources.append(source)
        return ResidentMemorySources(items=sources)


def _extract_candidates(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    llm: HarnessAgentLLMClient,
) -> ResidentMemoryExtraction:
    from octop_memory.pipeline.extractor import CandidateExtractor

    meta_key = f"resident_candidate_cursor_v1:{thread_id}"
    with _memory_control(services, owner_id, agent_id, thread_id):
        pass
    with _memory(services, owner_id, agent_id, thread_id) as memory:
        # This is an explicit recent-observation operation, not history ingestion.
        events = memory.list_raw(
            session_id=thread_id, thread_id=thread_id, event_type="user_message", limit=201
        )
        window = events[:200]
        prior = memory.get_meta(meta_key)
        seen = set(json.loads(prior)) if prior else set()
        pending = [event for event in window if event.id not in seen]
        batch = []
        budget, truncated = 0, 0
        for event in pending:
            content = event.content[:12000]
            size = len(content.encode("utf-8"))
            if budget + size > 64 * 1024:
                break
            batch.append(replace(event, content=content))
            budget += size
            truncated += int(content != event.content)
        response = ResidentMemoryExtraction(
            status="empty",
            pending_events=len(pending) - len(batch),
            has_earlier=len(events) > 200,
            truncated_events=truncated,
        )
        if not batch:
            return response
        try:
            result = CandidateExtractor(llm=llm).extract(batch, session_id=thread_id)
        except Exception:
            # Provider and parser failures must not expose the underlying private text.
            return response.model_copy(
                update={
                    "status": "failed",
                    "pending_events": len(pending),
                    "error_code": ErrorCode.RESIDENT_MEMORY_EXTRACTION_FAILED.value,
                }
            )
        if result.failure_reason:
            return response.model_copy(
                update={
                    "status": "failed",
                    "pending_events": len(pending),
                    "error_code": ErrorCode.RESIDENT_MEMORY_EXTRACTION_FAILED.value,
                }
            )
        consumed = {event.id for event in batch}
        with _memory_control(services, owner_id, agent_id, thread_id), memory.backend.transaction():
            # Namespace metadata and canonical candidates commit together. A fresh
            # process cannot automatically re-extract an already consumed window.
            current = memory.get_meta(meta_key)
            completed = set(json.loads(current)) if current else set()
            if consumed <= completed:
                return response
            memory.add_candidates(result.candidates)
            memory.set_meta(
                meta_key,
                json.dumps(sorted((completed | consumed) & {event.id for event in window})),
            )
        return response.model_copy(
            update={
                "status": "completed",
                "candidates": len(result.candidates),
                "events_extracted": len(batch),
            }
        )


async def extract_resident_candidates(
    services: SharedServices,
    registry: AgentManager,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    model_ref: str,
) -> ResidentMemoryExtraction:
    from octop_harness.memory.llm_client import HarnessAgentLLMClient

    await asyncio.to_thread(require_resident_binding, services, owner_id, agent_id, thread_id)
    if not model_ref or not registry.providers.is_model_ref_usable(model_ref):
        raise OctopError.localized(ErrorCode.RESIDENT_MODEL_UNAVAILABLE)
    try:
        agent = registry.get_agent(agent_id)
    except OctopError:
        await registry.start(agent_id)
        agent = registry.get_agent(agent_id)
    llm = HarnessAgentLLMClient(agent.model_factory, default_model=model_ref)
    return await asyncio.to_thread(
        _extract_candidates, services, owner_id, agent_id, thread_id, llm
    )


def resident_memory_context(
    services: SharedServices, owner_id: int, agent_id: str, thread_id: str
) -> str:
    """A bounded per-request snapshot; never capture or copy it into a Task."""
    with _memory(services, owner_id, agent_id, thread_id) as memory:
        atoms = memory.list_atoms(limit=20, include_deprecated=False)
        # JSON framing preserves user facts as data, rather than extra system instructions.
        facts: list[dict[str, Any]] = []
        for atom in atoms:
            fact = {"atom_id": atom.id, "assertion": atom.assertion[:2048]}
            candidate = [*facts, fact]
            if len(json.dumps(candidate, ensure_ascii=False).encode()) > 8192:
                break
            facts.append(fact)
        return json.dumps(facts, ensure_ascii=False) if facts else ""
