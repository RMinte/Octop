"""Global task matching with identical Unicode behavior across database engines."""

from __future__ import annotations

import unicodedata
from typing import Any

from octop.infra.db.repos._base import UNSET
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.services import SharedServices


def _fold(text: str) -> str:
    # Fold width without collapsing unrelated compatibility forms such as ①.
    width_folded = "".join(
        unicodedata.normalize("NFKC", char)
        if unicodedata.decomposition(char).startswith(("<wide>", "<narrow>"))
        else char
        for char in text
    )
    folded: list[str] = []
    strip_diacritics = False
    has_base = False
    for char in unicodedata.normalize("NFD", width_folded):
        if unicodedata.category(char).startswith("M"):
            # Keep script-specific signs (e.g. kana voicing and Arabic vowels),
            # while folding accents as Foundation does for the client search.
            if strip_diacritics or (has_base and "\u0300" <= char <= "\u036f"):
                continue
        else:
            name = unicodedata.name(char, "")
            strip_diacritics = name.startswith(("LATIN ", "GREEK ", "CYRILLIC "))
            has_base = True
        folded.append(char)
    # Remove accents before case folding: an attached Greek U+0345 is an
    # accent here, but casefold() would first turn it into a standalone iota.
    return unicodedata.normalize("NFC", "".join(folded).casefold())


def _contains(text: str, term: str) -> bool:
    start = text.find(term)
    while start != -1:
        end = start + len(term)
        # A retained mark belongs to its base; do not match only part of it.
        starts_at_base = start == 0 or not unicodedata.category(text[start]).startswith("M")
        ends_at_base = end == len(text) or not unicodedata.category(text[end]).startswith("M")
        if starts_at_base and ends_at_base:
            return True
        start = text.find(term, start + 1)
    return False


def page(
    conn: Any,
    services: SharedServices,
    owner_id: int,
    *,
    query: str,
    limit: int,
    boundary: tuple[int, int, str] | None,
    archived: bool,
    expert_id: str | None,
    status: str | None,
) -> list[ProjectTaskRow]:
    terms = _fold(query).split()
    project_names = services.project_repo.names_in_transaction(conn, owner_id) if terms else {}
    matches: list[ProjectTaskRow] = []
    batch_size = 100 if terms else limit
    # Keep memory bounded without limiting how far a sparse query can search.
    while len(matches) < limit:
        candidates = services.project_task_repo.page(
            conn,
            owner_id,
            None,
            batch_size,
            boundary,
            archived,
            scope="all",
            selected_expert_id=expert_id if expert_id is not None else UNSET,
            status=status,
        )
        if not terms:
            return candidates
        for row in candidates:
            text = _fold(row.title + " " + project_names.get(row.project_id or "", ""))
            if all(_contains(text, term) for term in terms):
                matches.append(row)
                if len(matches) == limit:
                    return matches
        if len(candidates) < batch_size:
            break
        last = candidates[-1]
        boundary = (int(last.pinned), last.updated_at, last.task_id)
    return matches
