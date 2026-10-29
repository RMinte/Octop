"""Conservative flagship-first ordering for automatic chat model selection.

Only recognized product families are versioned. Context size, arbitrary numbers,
and provider /models order are not evidence of model quality. Cross-family ties
keep their original order; this is a routing policy, not a benchmark leaderboard.
"""

from __future__ import annotations

import re


def _profile(model_id: str) -> tuple[str, int, tuple[int, ...]]:
    model = model_id.lower().strip()
    # Aggregators may retain an official vendor namespace in their model IDs.
    for prefix in ("openai/", "anthropic/", "google/", "z-ai/"):
        if model.startswith(prefix):
            model = model.removeprefix(prefix)
            break
    patterns = (
        ("glm", r"glm-(\d+(?:\.\d+){0,2})(v)?(?:-(turbo|flashx?|airx?))?"),
        ("gpt", r"gpt-(\d+(?:\.\d+){0,2})(?:-(pro|mini|nano))?(?:-\d{4}-\d{2}-\d{2})?"),
        ("claude", r"claude-(opus|sonnet|haiku)-(\d+(?:[.-]\d{1,2})?)(?:-\d{8})?"),
        ("gemini", r"gemini-(\d+(?:\.\d+){0,2})-(pro|argon|flash|flash-lite)(?:-preview)?"),
    )
    for family, pattern in patterns:
        match = re.fullmatch(pattern, model)
        if match is None:
            continue
        groups = match.groups()
        version = groups[1] if family == "claude" else groups[0]
        variant = groups[0] if family == "claude" else groups[-1]
        tier = 3
        if variant in {"sonnet", "turbo"}:
            tier = 2
        elif variant in {"mini", "nano", "haiku", "flash", "flashx", "flash-lite", "air", "airx"}:
            tier = 1
        parts = tuple(int(part) for part in version.replace("-", ".").split("."))
        return family, tier, parts + (0,) * (3 - len(parts))
    return "unknown", 0, (0, 0, 0)


def rank_model_refs(refs: list[str]) -> list[str]:
    """Prefer flagship tiers, then newer versions within each recognized family.

    Families at the same tier retain first-appearance precedence. Unknown names
    retain input order and remain usable fallbacks, without invented rankings.
    Eligibility, user preferences, modality and health belong to callers.
    """
    profiles = {ref: _profile(ref.partition("/")[2]) for ref in refs}
    families: dict[str, int] = {}
    for family, _, _ in profiles.values():
        families.setdefault(family, len(families))

    def key(ref: str) -> tuple[int, int, tuple[int, ...]]:
        family, tier, version = profiles[ref]
        return -tier, families[family], tuple(-part for part in version)

    return sorted(refs, key=key)
