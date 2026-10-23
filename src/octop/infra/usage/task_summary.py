"""Task/source totals with explicit unknowns and separate retained journals."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from octop.infra.db.repos.model_calls import TOKEN_FIELDS, AttributedTaskModelCall
from octop.infra.db.repos.usage import (
    RetainedTaskUsage,
    RetainedTaskUsageEntry,
    resolve_usage_window,
)
from octop.infra.usage.pricing import (
    LONG_CONTEXT_RATES,
    LONG_CONTEXT_THRESHOLD,
    PRICING_ASSUMPTIONS,
    PRICING_CHECKED_AT,
    PRICING_CURRENCY,
    PRICING_SOURCE_URLS,
    PRICING_VERSION,
    REFERENCE_PRICING_MODEL,
    SHORT_CONTEXT_RATES,
    ReferenceCost,
    ReferenceRates,
    compute_reference_cost,
    compute_retained_reference_cost,
)

if TYPE_CHECKING:
    from octop.infra.db.services import SharedServices


class TaskUsageFilters(BaseModel):
    window: str = Field(default="last_30d", max_length=64)
    task_id: str | None = Field(default=None, max_length=256)
    project_id: str | None = Field(default=None, max_length=256)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)


class TaskTokenTotals(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    uncached_input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None


class TaskTokenCounts(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    uncached_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


TokenCoverage = Literal["recorded", "partial", "not_collected", "no_data"]


class TaskTokenCoverage(BaseModel):
    input_tokens: TokenCoverage
    output_tokens: TokenCoverage
    total_tokens: TokenCoverage
    uncached_input_tokens: TokenCoverage
    cache_read_tokens: TokenCoverage
    cache_write_tokens: TokenCoverage
    reasoning_tokens: TokenCoverage


class ReferenceCostAmounts(BaseModel):
    amount_usd: str | None = Field(
        description="Complete reference estimate; null if any recorded call cannot be fully priced."
    )
    known_amount_usd: str | None = Field(
        description="Known subtotal; never an actual supplier bill."
    )
    status: Literal["estimated", "estimated_range", "partial", "unknown", "no_data"]
    lower_bound_usd: str | None
    upper_bound_usd: str | None
    missing_fields: list[str]
    assumptions: list[str]


class ReferenceCostTotals(ReferenceCostAmounts):
    estimated_calls: int
    estimated_range_calls: int
    partial_calls: int
    unknown_calls: int


class RetainedReferenceCost(ReferenceCostAmounts):
    estimated_records: int
    estimated_range_records: int
    partial_records: int
    unknown_records: int


class TaskCallUsage(BaseModel):
    calls: int
    statuses: dict[str, int]
    tokens: TaskTokenTotals
    missing_calls: TaskTokenCounts = Field(description="Calls with no known value for each field.")
    derived_calls: TaskTokenCounts = Field(
        description="Known values not explicitly adapter-reported."
    )
    coverage: TaskTokenCoverage
    attribution_counts: dict[str, int]
    reference_cost: ReferenceCostTotals


class TaskSourceUsage(BaseModel):
    box: TaskCallUsage
    third_party: TaskCallUsage


class RetainedSourceTotals(BaseModel):
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    usage_records: int
    source_basis: Literal["configuration_inference", "unattributed", "no_data"]
    reference_cost: RetainedReferenceCost


class RetainedSources(BaseModel):
    box: RetainedSourceTotals
    third_party: RetainedSourceTotals


class RetainedTaskUsageTotals(BaseModel):
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    turns: int | None
    usage_records: int
    coverage: Literal["recorded", "no_data"]
    source_attribution: Literal["inferred", "partial", "unknown", "no_data"]
    sources: RetainedSources
    unattributed_usage: RetainedSourceTotals
    reference_cost: RetainedReferenceCost
    measurement_unit: Literal["retained_task_journal"] = "retained_task_journal"
    cannot_add_to_call_usage: Literal[True] = True


class TaskUsageTotals(BaseModel):
    call_usage: TaskCallUsage
    sources: TaskSourceUsage
    retained_task_usage: RetainedTaskUsageTotals


class TaskUsageItem(TaskUsageTotals):
    task_id: str
    title: str
    project_id: str
    project_name: str
    last_usage_at: int = Field(description="Latest included call/journal time, Unix milliseconds.")


class ReferencePricingRates(BaseModel):
    input: str
    cache_read: str
    cache_write_30m: str
    cache_write_12h: str
    output: str


class ReferencePricing(BaseModel):
    model: str = REFERENCE_PRICING_MODEL
    version: str = PRICING_VERSION
    checked_at: str = PRICING_CHECKED_AT
    currency: str = PRICING_CURRENCY
    basis: Literal["openai_reference_estimate"] = "openai_reference_estimate"
    service_tier: Literal["standard"] = "standard"
    rate_unit: Literal["USD_per_million_tokens"] = "USD_per_million_tokens"
    long_context_threshold: int = LONG_CONTEXT_THRESHOLD
    short_context: ReferencePricingRates
    long_context: ReferencePricingRates
    source_urls: list[str] = Field(default_factory=lambda: list(PRICING_SOURCE_URLS))
    assumptions: list[str] = Field(default_factory=lambda: list(PRICING_ASSUMPTIONS))


class TaskUsageCapabilities(BaseModel):
    source_breakdown: Literal[True] = True
    reference_cost: Literal[True] = True
    historical_source_inference: Literal[True] = True
    actual_cost: Literal[False] = False
    execution_location: Literal[False] = False
    savings: Literal[False] = False
    budget: Literal[False] = False


class TaskUsagePage(BaseModel):
    window: str
    range_start: int = Field(description="Inclusive Unix milliseconds.")
    range_end: int = Field(description="Exclusive Unix milliseconds.")
    timezone: str
    generated_at: int = Field(description="Unix milliseconds; not a frozen accounting snapshot.")
    account_scope: Literal["user", "all_users"]
    user_id: int | None
    measurement_unit: Literal["logical_model_invocation"] = "logical_model_invocation"
    recording_scope: Literal["retained_records_only"] = "retained_records_only"
    history_complete: Literal[False] = False
    physical_attempts_available: Literal[False] = False
    capabilities: TaskUsageCapabilities = Field(default_factory=TaskUsageCapabilities)
    pricing: ReferencePricing
    total_tasks: int
    offset: int
    limit: int
    next_offset: int | None
    totals: TaskUsageTotals = Field(description="All matched tasks, before pagination.")
    unassigned_call_usage: TaskCallUsage = Field(
        description="Calls without an unambiguous accepted task; excluded from task totals."
    )
    unassigned_sources: TaskSourceUsage
    items: list[TaskUsageItem]


def _amount(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _cost_totals(costs: Sequence[ReferenceCost]) -> ReferenceCostTotals:
    counts = Counter(cost.status for cost in costs)
    known = [Decimal(cost.known_amount_usd) for cost in costs if cost.known_amount_usd is not None]
    subtotal = _amount(sum(known, Decimal(0))) if known else None
    lower_parts = [
        cost.lower_bound_usd or cost.amount_usd or cost.known_amount_usd for cost in costs
    ]
    upper_parts = [cost.upper_bound_usd or cost.amount_usd for cost in costs]
    lower = (
        _amount(sum((Decimal(part) for part in lower_parts if part is not None), Decimal(0)))
        if any(part is not None for part in lower_parts)
        else None
    )
    upper = (
        _amount(sum((Decimal(part) for part in upper_parts if part is not None), Decimal(0)))
        if costs and all(part is not None for part in upper_parts)
        else None
    )
    status: Literal["estimated", "estimated_range", "partial", "unknown", "no_data"] = (
        "no_data"
        if not costs
        else "estimated"
        if counts["estimated"] == len(costs)
        else "estimated_range"
        if counts["estimated"] + counts["estimated_range"] == len(costs)
        else "partial"
        if lower is not None
        else "unknown"
    )
    return ReferenceCostTotals(
        amount_usd=subtotal if status == "estimated" else None,
        known_amount_usd=subtotal,
        status=status,
        lower_bound_usd=lower,
        upper_bound_usd=upper,
        estimated_calls=counts["estimated"],
        estimated_range_calls=counts["estimated_range"],
        partial_calls=counts["partial"],
        unknown_calls=counts["unknown"],
        missing_fields=sorted({field for cost in costs for field in cost.missing_fields}),
        assumptions=sorted({value for cost in costs for value in cost.assumptions}),
    )


def _call_usage(
    calls: Sequence[AttributedTaskModelCall],
    usages: dict[str, dict[str, object]],
    costs: dict[str, ReferenceCost],
) -> TaskCallUsage:
    values: dict[str, int | None] = {}
    missing: dict[str, int] = {}
    derived: dict[str, int] = {}
    coverage: dict[str, TokenCoverage] = {}
    for field in TOKEN_FIELDS:
        known: list[int] = []
        derived[field] = 0
        for call in calls:
            usage = usages[call.row.call_id]
            value = usage.get(field)
            if type(value) is int:
                known.append(value)
                presence = usage.get("presence")
                if isinstance(presence, dict) and not presence.get(field, False):
                    derived[field] += 1
        values[field] = sum(known) if known else None
        missing[field] = len(calls) - len(known)
        coverage[field] = (
            "no_data"
            if not calls
            else "not_collected"
            if not known
            else "partial"
            if missing[field]
            else "recorded"
        )
    return TaskCallUsage(
        calls=len(calls),
        statuses=dict(Counter(call.row.status for call in calls)),
        tokens=TaskTokenTotals.model_validate(values),
        missing_calls=TaskTokenCounts.model_validate(missing),
        derived_calls=TaskTokenCounts.model_validate(derived),
        coverage=TaskTokenCoverage.model_validate(coverage),
        attribution_counts=dict(Counter(call.attribution for call in calls)),
        reference_cost=_cost_totals([costs[call.row.call_id] for call in calls]),
    )


def _sources(
    calls: Sequence[AttributedTaskModelCall],
    usages: dict[str, dict[str, object]],
    costs: dict[str, ReferenceCost],
) -> TaskSourceUsage:
    return TaskSourceUsage(
        box=_call_usage([call for call in calls if call.row.source == "box"], usages, costs),
        third_party=_call_usage(
            [call for call in calls if call.row.source == "third_party"], usages, costs
        ),
    )


def _retained_cost(entries: Sequence[RetainedTaskUsageEntry]) -> RetainedReferenceCost:
    costs = [
        compute_retained_reference_cost(
            {
                "input_tokens": entry.input_tokens,
                "output_tokens": entry.output_tokens,
            }
        )
        for entry in entries
    ]
    totals = _cost_totals(costs)
    return RetainedReferenceCost(
        **totals.model_dump(include=set(ReferenceCostAmounts.model_fields)),
        estimated_records=totals.estimated_calls,
        estimated_range_records=totals.estimated_range_calls,
        partial_records=totals.partial_calls,
        unknown_records=totals.unknown_calls,
    )


def _retained_source(
    entries: Sequence[RetainedTaskUsageEntry], *, attributed: bool
) -> RetainedSourceTotals:
    return RetainedSourceTotals(
        input_tokens=sum(entry.input_tokens for entry in entries) if entries else None,
        output_tokens=sum(entry.output_tokens for entry in entries) if entries else None,
        total_tokens=sum(entry.total_tokens for entry in entries) if entries else None,
        usage_records=len(entries),
        source_basis="no_data"
        if not entries
        else "configuration_inference"
        if attributed
        else "unattributed",
        reference_cost=_retained_cost(entries),
    )


def _retained(
    rows: Sequence[RetainedTaskUsage], entries: Sequence[RetainedTaskUsageEntry]
) -> RetainedTaskUsageTotals:
    box = [entry for entry in entries if entry.provider_kind == "box"]
    third_party = [
        entry
        for entry in entries
        if entry.provider_kind is not None and entry.provider_kind != "box"
    ]
    unknown = [entry for entry in entries if entry.provider_kind is None]
    return RetainedTaskUsageTotals(
        input_tokens=sum(row.input_tokens for row in rows) if rows else None,
        output_tokens=sum(row.output_tokens for row in rows) if rows else None,
        total_tokens=sum(row.total_tokens for row in rows) if rows else None,
        turns=sum(row.turns for row in rows) if rows else None,
        usage_records=sum(row.usage_records for row in rows),
        coverage="recorded" if rows else "no_data",
        source_attribution=(
            "no_data"
            if not rows
            else "unknown"
            if not box and not third_party
            else "partial"
            if unknown
            else "inferred"
        ),
        sources=RetainedSources(
            box=_retained_source(box, attributed=True),
            third_party=_retained_source(third_party, attributed=True),
        ),
        unattributed_usage=_retained_source(unknown, attributed=False),
        reference_cost=_retained_cost(entries),
    )


def _rates(rates: ReferenceRates) -> ReferencePricingRates:
    return ReferencePricingRates(
        **{field: str(getattr(rates, field)) for field in ReferencePricingRates.model_fields}
    )


def _task_summary(
    services: SharedServices, *, user_id: int | None, filters: TaskUsageFilters, timezone: str
) -> TaskUsagePage:
    try:
        timezone = ZoneInfo(timezone).key
    except ZoneInfoNotFoundError:
        timezone = "UTC"
    if filters.window not in {"today", "yesterday", "last_7d", "last_30d", "all"} and not (
        filters.window.startswith(("day:", "month:", "range:"))
    ):
        raise ValueError("Invalid usage window")
    start, end = resolve_usage_window(filters.window, timezone=timezone)
    calls = services.model_call_repo.list_task_usage(
        user_id=user_id,
        range_start=start * 1000,
        range_end=end * 1000,
        task_id=filters.task_id,
        project_id=filters.project_id,
    )
    retained, retained_entries = services.usage_repo.read_retained_task_usage(
        user_id=user_id,
        range_start=start,
        range_end=end,
        task_id=filters.task_id,
        project_id=filters.project_id,
    )
    usages = {call.row.call_id: json.loads(call.row.usage_json) for call in calls}
    costs = {key: compute_reference_cost(usage) for key, usage in usages.items()}
    groups: dict[str, list[AttributedTaskModelCall]] = defaultdict(list)
    for call in calls:
        groups[call.task_id].append(call)
    journals = {row.task_id: row for row in retained}
    journal_entries: dict[str, list[RetainedTaskUsageEntry]] = defaultdict(list)
    for entry in retained_entries:
        journal_entries[entry.task_id].append(entry)
    items: list[TaskUsageItem] = []
    for task_id in groups.keys() | journals.keys():
        if not task_id:
            continue
        task_calls = groups[task_id]
        journal = journals.get(task_id)
        latest = task_calls[-1] if task_calls else None
        items.append(
            TaskUsageItem(
                task_id=task_id,
                title=journal.task_title if journal else latest.task_title if latest else "",
                project_id=journal.project_id if journal else latest.project_id if latest else "",
                project_name=journal.project_name
                if journal
                else latest.project_name
                if latest
                else "",
                last_usage_at=max(
                    latest.row.started_at if latest else 0,
                    journal.last_usage_at * 1000 if journal else 0,
                ),
                call_usage=_call_usage(task_calls, usages, costs),
                sources=_sources(task_calls, usages, costs),
                retained_task_usage=_retained(
                    [journal] if journal else [], journal_entries[task_id]
                ),
            )
        )
    items.sort(key=lambda item: (item.last_usage_at, item.task_id), reverse=True)
    assigned = [call for call in calls if call.task_id]
    unassigned = groups[""]
    next_offset = filters.offset + filters.limit
    return TaskUsagePage(
        window=filters.window,
        range_start=start * 1000,
        range_end=end * 1000,
        timezone=timezone,
        generated_at=time.time_ns() // 1_000_000,
        account_scope="all_users" if user_id is None else "user",
        user_id=user_id,
        pricing=ReferencePricing(
            short_context=_rates(SHORT_CONTEXT_RATES), long_context=_rates(LONG_CONTEXT_RATES)
        ),
        total_tasks=len(items),
        offset=filters.offset,
        limit=filters.limit,
        next_offset=next_offset if next_offset < len(items) else None,
        totals=TaskUsageTotals(
            call_usage=_call_usage(assigned, usages, costs),
            sources=_sources(assigned, usages, costs),
            retained_task_usage=_retained(retained, retained_entries),
        ),
        unassigned_call_usage=_call_usage(unassigned, usages, costs),
        unassigned_sources=_sources(unassigned, usages, costs),
        items=items[filters.offset : next_offset],
    )


async def task_usage_summary(
    services: SharedServices, *, user_id: int | None, filters: TaskUsageFilters, timezone: str
) -> TaskUsagePage:
    return await asyncio.to_thread(
        _task_summary, services, user_id=user_id, filters=filters, timezone=timezone
    )
