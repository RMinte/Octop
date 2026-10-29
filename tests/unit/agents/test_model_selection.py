"""Automatic selection orders product tiers without inventing cross-vendor scores."""

import pytest

from octop.infra.agents.providers.selection import rank_model_refs


@pytest.mark.parametrize(
    ("models", "expected"),
    [
        (
            ["glm-4.7-flash", "glm-5", "glm-5.1", "glm-5.2", "glm-5-turbo", "glm-5v-turbo"],
            ["glm-5.2", "glm-5.1", "glm-5", "glm-5-turbo", "glm-5v-turbo", "glm-4.7-flash"],
        ),
        (["glm-5.9", "glm-5.10", "glm-6-flash"], ["glm-5.10", "glm-5.9", "glm-6-flash"]),
        (["gpt-5.5-mini", "gpt-5.4", "gpt-5.5"], ["gpt-5.5", "gpt-5.4", "gpt-5.5-mini"]),
        (
            ["claude-sonnet-5", "claude-opus-4-5-20251101", "claude-opus-4-6"],
            ["claude-opus-4-6", "claude-opus-4-5-20251101", "claude-sonnet-5"],
        ),
        (
            ["gemini-3.8-flash", "gemini-3.1-pro-preview", "gemini-4-argon"],
            ["gemini-4-argon", "gemini-3.1-pro-preview", "gemini-3.8-flash"],
        ),
        (
            ["custom-900b-2099", "RM-01 VLM", "glm-5.2-custom", "my-gpt-99"],
            ["custom-900b-2099", "RM-01 VLM", "glm-5.2-custom", "my-gpt-99"],
        ),
    ],
)
def test_family_tier_then_numeric_version(models: list[str], expected: list[str]) -> None:
    assert rank_model_refs([f"provider/{model}" for model in models]) == [
        f"provider/{model}" for model in expected
    ]


def test_cross_family_versions_are_not_comparable() -> None:
    refs = ["one/glm-5.1", "two/gpt-6", "three/glm-5.2", "four/anthropic/claude-opus-5"]
    assert rank_model_refs(refs) == [refs[2], refs[0], refs[1], refs[3]]


def test_equal_versions_and_unknown_names_remain_stable() -> None:
    refs = ["one/glm-5", "two/glm-5.0", "three/unknown-b", "four/unknown-a"]
    assert rank_model_refs(refs) == refs
