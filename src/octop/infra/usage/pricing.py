"""Token-equivalent GPT-6 Astra estimates, never supplier billing receipts.

Prices were checked against the official model, pricing, prompt-caching and
reasoning documentation on 2026-10-23. Historical calls are repriced using this
snapshot. All providers, including Box, use this same comparison model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

REFERENCE_PRICING_MODEL = "gpt-6-astra"
PRICING_VERSION = "openai-gpt-6-astra-standard-2026-10-23-v1"
PRICING_CHECKED_AT = "2026-10-23"
PRICING_CURRENCY = "USD"
PRICING_SOURCE_URLS = (
    "https://developers.openai.com/api/docs/models/gpt-6-astra",
    "https://developers.openai.com/api/docs/pricing",
    "https://developers.openai.com/api/docs/guides/prompt-caching",
    "https://developers.openai.com/api/docs/guides/reasoning",
)
LONG_CONTEXT_THRESHOLD = 272_000
PRICING_ASSUMPTIONS = (
    "all_sources_use_reference_model",
    "standard_service_tier",
    "historical_usage_repriced_at_snapshot",
    "reasoning_already_included_in_output_tokens",
    "excludes_tool_fees_storage_taxes_and_regional_uplifts",
)
_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ReferenceRates:
    """USD per million tokens; writes replace ordinary input pricing."""

    input: Decimal
    cache_read: Decimal
    cache_write_30m: Decimal
    cache_write_12h: Decimal
    output: Decimal


SHORT_CONTEXT_RATES = ReferenceRates(
    input=Decimal("10"),
    cache_read=Decimal("1"),
    cache_write_30m=Decimal("12.50"),
    cache_write_12h=Decimal("25"),
    output=Decimal("50"),
)
LONG_CONTEXT_RATES = ReferenceRates(
    input=Decimal("20"),
    cache_read=Decimal("2"),
    cache_write_30m=Decimal("25"),
    cache_write_12h=Decimal("50"),
    output=Decimal("75"),
)


@dataclass(frozen=True)
class ReferenceCost:
    """A partial subtotal must never be presented as a complete call cost."""

    amount_usd: str | None
    known_amount_usd: str | None
    status: Literal["estimated", "estimated_range", "partial", "unknown"]
    context_band: Literal["short", "long", "unknown"]
    missing_fields: tuple[str, ...]
    assumptions: tuple[str, ...]
    lower_bound_usd: str | None = None
    upper_bound_usd: str | None = None


def _count(usage: Mapping[str, object], field: str) -> int | None:
    value = usage.get(field)
    return value if type(value) is int and value >= 0 else None


def _amount(value: Decimal) -> str:
    return format(value.normalize(), "f")


def compute_reference_cost(usage: Mapping[str, object]) -> ReferenceCost:
    """Price reported tokens using one fixed Standard reference schedule.

    The recorded input count selects the band for the entire call (>272,000
    uses long-context rates). Reasoning is already part of output. Reported
    cache writes use the reference API's default 30-minute lifetime because
    receipts do not retain TTL. Unreported writes are not simulated: the
    non-read input remainder uses ordinary input pricing. Neither assumption
    changes the underlying usage or claims that an actual write count is zero.

    Input, output and cache-read counts must be known for a complete estimate.
    Exact zero input or a complete input breakdown can resolve missing counts.
    Cancellation/failure status is intentionally irrelevant: reported usage
    costs the same, while missing usage remains unknown.
    """
    input_tokens = _count(usage, "input_tokens")
    output_tokens = _count(usage, "output_tokens")
    cached = _count(usage, "cache_read_tokens")
    written = _count(usage, "cache_write_tokens")
    uncached = _count(usage, "uncached_input_tokens")
    invalid_breakdown = any(
        usage.get(field) is not None and _count(usage, field) is None
        for field in ("cache_read_tokens", "cache_write_tokens", "uncached_input_tokens")
    )
    assumptions = list(PRICING_ASSUMPTIONS)
    missing: list[str] = []

    if input_tokens is None and uncached is not None and cached is not None and written is not None:
        input_tokens = uncached + cached + written
    if input_tokens is None:
        return ReferenceCost(
            amount_usd=None,
            known_amount_usd=None,
            status="unknown",
            context_band="unknown",
            missing_fields=("input_tokens",)
            + (("output_tokens",) if output_tokens is None else ()),
            assumptions=tuple(assumptions),
        )

    long_context = input_tokens > LONG_CONTEXT_THRESHOLD
    rates = LONG_CONTEXT_RATES if long_context else SHORT_CONTEXT_RATES
    components: list[Decimal] = []
    if output_tokens is None:
        missing.append("output_tokens")
    else:
        components.append(Decimal(output_tokens) * rates.output / _MILLION)

    if input_tokens == 0 and cached is None:
        cached = 0
    if cached is None and uncached is not None and written is not None:
        cached = input_tokens - uncached - written
    if written is None and uncached is not None and cached is not None:
        written = input_tokens - uncached - cached
    if invalid_breakdown:
        missing.append("valid_input_breakdown")
    elif cached is None:
        missing.append("cache_read_tokens")
    elif (
        cached < 0
        or cached > input_tokens
        or (written is not None and (written < 0 or cached + written > input_tokens))
        or (
            uncached is not None
            and written is not None
            and uncached + cached + written != input_tokens
        )
    ):
        missing.append("valid_input_breakdown")
    else:
        if written is None:
            assumptions.append("unreported_cache_writes_not_simulated")
            written_for_estimate = 0
        else:
            written_for_estimate = written
            if written:
                assumptions.append("cache_write_ttl_assumed_30m")
        regular = input_tokens - cached - written_for_estimate
        components.append(
            (
                Decimal(regular) * rates.input
                + Decimal(cached) * rates.cache_read
                + Decimal(written_for_estimate) * rates.cache_write_30m
            )
            / _MILLION
        )

    known_amount = _amount(sum(components, Decimal(0))) if components else None
    return ReferenceCost(
        amount_usd=known_amount if not missing else None,
        known_amount_usd=known_amount,
        status="partial" if missing and components else "unknown" if missing else "estimated",
        context_band="long" if long_context else "short",
        missing_fields=tuple(missing),
        assumptions=tuple(assumptions),
    )


def compute_retained_reference_cost(usage: Mapping[str, object]) -> ReferenceCost:
    """Estimate a retained journal without mistaking its sum for one call.

    A journal may combine many model invocations. If its total input is at most
    the short-context threshold, every constituent invocation is also short.
    Above that bound, known input/output totals bound the reference cost between
    all-short and all-long rates without choosing actual per-call context bands.
    Missing cache details receive no reference discount and no simulated cache
    writes; this is an estimation assumption, not a backfill of recorded usage.
    """
    input_tokens = _count(usage, "input_tokens")
    output_tokens = _count(usage, "output_tokens")
    assumptions = (
        "retained_journal_usage",
        "unreported_cache_reads_assumed_uncached",
        "unreported_cache_writes_not_simulated",
    )
    if input_tokens is not None and input_tokens > LONG_CONTEXT_THRESHOLD:
        lower_bound = upper_bound = None
        if output_tokens is not None:
            lower_bound = _amount(
                (
                    Decimal(input_tokens) * SHORT_CONTEXT_RATES.input
                    + Decimal(output_tokens) * SHORT_CONTEXT_RATES.output
                )
                / _MILLION
            )
            upper_bound = _amount(
                (
                    Decimal(input_tokens) * LONG_CONTEXT_RATES.input
                    + Decimal(output_tokens) * LONG_CONTEXT_RATES.output
                )
                / _MILLION
            )
        return ReferenceCost(
            amount_usd=None,
            known_amount_usd=None,
            status="estimated_range" if output_tokens is not None else "unknown",
            context_band="unknown",
            missing_fields=("per_call_context",)
            + (("output_tokens",) if output_tokens is None else ()),
            assumptions=(
                *PRICING_ASSUMPTIONS,
                *assumptions,
                *(("retained_context_band_range",) if output_tokens is not None else ()),
            ),
            lower_bound_usd=lower_bound,
            upper_bound_usd=upper_bound,
        )
    result = compute_reference_cost(
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
    )
    return replace(result, assumptions=(*result.assumptions, *assumptions))
