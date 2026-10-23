"""Contract checks for the fixed GPT-6 Astra reference estimate."""

from decimal import Decimal

import pytest

from octop.infra.usage.pricing import (
    LONG_CONTEXT_RATES,
    PRICING_CHECKED_AT,
    PRICING_CURRENCY,
    REFERENCE_PRICING_MODEL,
    SHORT_CONTEXT_RATES,
    compute_reference_cost,
    compute_retained_reference_cost,
)


def test_recorded_tokens_can_be_compared_without_inventing_cache_writes() -> None:
    usage = {"input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": 400}
    result = compute_reference_cost(usage)
    assert result.status == "estimated"
    assert Decimal(result.amount_usd or "NaN") == Decimal("0.0114")
    assert "unreported_cache_writes_not_simulated" in result.assumptions
    assert "cache_write_tokens" not in usage


def test_cache_writes_replace_regular_input_and_use_disclosed_default_ttl() -> None:
    result = compute_reference_cost(
        {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_read_tokens": 400,
            "cache_write_tokens": 100,
        }
    )
    assert result.status == "estimated"
    assert Decimal(result.amount_usd or "NaN") == Decimal("0.01165")
    assert "cache_write_ttl_assumed_30m" in result.assumptions


@pytest.mark.parametrize(
    ("input_tokens", "band", "expected"),
    [(272_000, "short", "2.725"), (272_001, "long", "5.44752")],
)
def test_context_threshold_prices_the_entire_call(
    input_tokens: int, band: str, expected: str
) -> None:
    result = compute_reference_cost(
        {"input_tokens": input_tokens, "output_tokens": 100, "cache_read_tokens": 0}
    )
    assert result.context_band == band
    assert Decimal(result.amount_usd or "NaN") == Decimal(expected)


def test_long_context_uses_cache_and_output_multipliers() -> None:
    result = compute_reference_cost(
        {
            "input_tokens": 300_000,
            "output_tokens": 1000,
            "cache_read_tokens": 100_000,
            "cache_write_tokens": 100_000,
        }
    )
    assert Decimal(result.amount_usd or "NaN") == Decimal("4.775")


def test_reasoning_is_not_billed_twice() -> None:
    usage = {"input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": 0}
    assert (
        compute_reference_cost(usage).amount_usd
        == compute_reference_cost({**usage, "reasoning_tokens": 80}).amount_usd
    )


def test_unknown_usage_is_not_free() -> None:
    result = compute_reference_cost({})
    assert result.status == "unknown"
    assert result.amount_usd is None
    assert result.known_amount_usd is None
    assert result.context_band == "unknown"


def test_output_without_input_does_not_guess_a_context_band() -> None:
    result = compute_reference_cost({"output_tokens": 100})
    assert result.status == "unknown"
    assert result.known_amount_usd is None
    assert result.missing_fields == ("input_tokens",)


@pytest.mark.parametrize(
    ("usage", "field", "subtotal"),
    [
        ({"input_tokens": 1000, "cache_read_tokens": 0}, "output_tokens", "0.01"),
        ({"input_tokens": 1000, "output_tokens": 100}, "cache_read_tokens", "0.005"),
    ],
)
def test_missing_required_counts_leave_only_a_partial_subtotal(
    usage: dict[str, object], field: str, subtotal: str
) -> None:
    result = compute_reference_cost(usage)
    assert result.status == "partial"
    assert result.amount_usd is None
    assert result.missing_fields == (field,)
    assert Decimal(result.known_amount_usd or "NaN") == Decimal(subtotal)


def test_reported_zero_remains_an_estimate_of_zero() -> None:
    result = compute_reference_cost({"input_tokens": 0, "output_tokens": 0})
    assert result.status == "estimated"
    assert result.amount_usd == "0"


@pytest.mark.parametrize("cached", [1001, -1, True])
def test_invalid_input_breakdown_never_produces_a_full_estimate(cached: object) -> None:
    result = compute_reference_cost(
        {"input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": cached}
    )
    assert result.status == "partial"
    assert result.amount_usd is None
    assert Decimal(result.known_amount_usd or "NaN") == Decimal("0.005")


@pytest.mark.parametrize("written", [-1, True, 1001])
def test_invalid_write_count_is_not_an_assumption_of_no_writes(written: object) -> None:
    result = compute_reference_cost(
        {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_read_tokens": 0,
            "cache_write_tokens": written,
        }
    )
    assert result.status == "partial"
    assert result.missing_fields == ("valid_input_breakdown",)
    assert "unreported_cache_writes_not_simulated" not in result.assumptions


def test_complete_input_breakdown_can_resolve_missing_totals() -> None:
    result = compute_reference_cost(
        {
            "uncached_input_tokens": 500,
            "cache_read_tokens": 400,
            "cache_write_tokens": 100,
            "output_tokens": 100,
        }
    )
    assert result.status == "estimated"
    assert Decimal(result.amount_usd or "NaN") == Decimal("0.01165")


def test_known_input_breakdown_derives_writes_without_discarding_them() -> None:
    result = compute_reference_cost(
        {
            "input_tokens": 1000,
            "uncached_input_tokens": 500,
            "cache_read_tokens": 400,
            "output_tokens": 100,
        }
    )
    assert Decimal(result.amount_usd or "NaN") == Decimal("0.01165")
    assert "cache_write_ttl_assumed_30m" in result.assumptions


def test_public_snapshot_records_default_and_extended_write_rates() -> None:
    assert REFERENCE_PRICING_MODEL == "gpt-6-astra"
    assert PRICING_CURRENCY == "USD"
    assert PRICING_CHECKED_AT == "2026-10-23"
    assert SHORT_CONTEXT_RATES.cache_write_12h == Decimal("25")
    assert LONG_CONTEXT_RATES.cache_write_12h == Decimal("50")


def test_retained_short_journal_uses_regular_input_price_without_changing_usage() -> None:
    usage = {"input_tokens": 1000, "output_tokens": 100}
    result = compute_retained_reference_cost(usage)
    assert result.status == "estimated"
    assert result.context_band == "short"
    assert Decimal(result.amount_usd or "NaN") == Decimal("0.015")
    assert usage == {"input_tokens": 1000, "output_tokens": 100}
    assert "retained_journal_usage" in result.assumptions
    assert "unreported_cache_reads_assumed_uncached" in result.assumptions
    assert "unreported_cache_writes_not_simulated" in result.assumptions
    assert compute_reference_cost(usage).status == "partial"


def test_retained_real_zero_is_not_unknown() -> None:
    result = compute_retained_reference_cost({"input_tokens": 0, "output_tokens": 0})
    assert result.status == "estimated"
    assert result.amount_usd == "0"


@pytest.mark.parametrize(
    ("input_tokens", "lower", "upper"),
    [(272_001, "2.72501", "5.44752"), (900_000, "9.005", "18.0075")],
)
def test_retained_large_input_bounds_cost_without_choosing_per_call_context(
    input_tokens: int, lower: str, upper: str
) -> None:
    result = compute_retained_reference_cost({"input_tokens": input_tokens, "output_tokens": 100})
    assert result.status == "estimated_range"
    assert result.amount_usd is None
    assert result.known_amount_usd is None
    assert result.context_band == "unknown"
    assert result.missing_fields == ("per_call_context",)
    assert Decimal(result.lower_bound_usd or "NaN") == Decimal(lower)
    assert Decimal(result.upper_bound_usd or "NaN") == Decimal(upper)
    assert "retained_context_band_range" in result.assumptions


def test_retained_input_at_threshold_bounds_every_call_to_short_context() -> None:
    result = compute_retained_reference_cost({"input_tokens": 272_000, "output_tokens": 100})
    assert result.status == "estimated"
    assert Decimal(result.amount_usd or "NaN") == Decimal("2.725")


def test_retained_reasoning_is_already_in_output() -> None:
    usage = {"input_tokens": 1000, "output_tokens": 100}
    assert (
        compute_retained_reference_cost(usage).amount_usd
        == compute_retained_reference_cost({**usage, "reasoning_tokens": 80}).amount_usd
    )


def test_retained_missing_input_cannot_establish_context_band() -> None:
    result = compute_retained_reference_cost({"output_tokens": 100})
    assert result.status == "unknown"
    assert result.amount_usd is None
    assert result.missing_fields == ("input_tokens",)
    assert result.upper_bound_usd is None


def test_retained_missing_output_leaves_input_as_partial_reference_cost() -> None:
    result = compute_retained_reference_cost({"input_tokens": 1000})
    assert result.status == "partial"
    assert result.amount_usd is None
    assert Decimal(result.known_amount_usd or "NaN") == Decimal("0.01")
    assert result.missing_fields == ("output_tokens",)
    assert result.upper_bound_usd is None


def test_retained_large_input_without_output_has_no_closed_cost_range() -> None:
    result = compute_retained_reference_cost({"input_tokens": 900_000})
    assert result.status == "unknown"
    assert result.amount_usd is None
    assert result.lower_bound_usd is None
    assert result.upper_bound_usd is None
    assert result.missing_fields == ("per_call_context", "output_tokens")


def test_retained_range_does_not_add_reasoning_twice() -> None:
    usage = {"input_tokens": 900_000, "output_tokens": 100}
    assert compute_retained_reference_cost(usage) == compute_retained_reference_cost(
        {**usage, "reasoning_tokens": 80}
    )
