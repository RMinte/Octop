"""Small, deterministic policies shared by task admission and semantic naming."""

from __future__ import annotations

import re


def provisional_title(prompt: str) -> str:
    return next(" ".join(line.split()) for line in prompt.splitlines() if line.strip())[:60]


def normalized_title(raw: str) -> str | None:
    title = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    title = re.sub(r"^(?:title|标题)\s*[:：]\s*", "", title, flags=re.IGNORECASE)
    title = " ".join(title.strip(" \t#*-•`'\"“”‘’《》。.!！?？:：;；").split())
    return title[:60] or None
