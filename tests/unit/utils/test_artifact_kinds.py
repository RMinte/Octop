"""File kind labels and SQL filters must agree for the fixed rule catalog."""

from __future__ import annotations

import sqlite3

from octop.infra.db.repos.project_task_artifacts import _KIND_PARAMS, _KIND_SQL
from octop.infra.utils.artifact_kinds import (
    ARTIFACT_KIND_RULES,
    artifact_business_category,
    artifact_kind,
    ascii_lower,
)


def test_ascii_case_folding_preserves_non_ascii_characters():
    assert ascii_lower("REPORT ÄİΣ报告") == "report ÄİΣ报告"


def test_sql_and_metadata_kind_agree_for_every_extension_and_mime():
    examples = []
    for kind, extensions, media_types in ARTIFACT_KIND_RULES:
        examples.extend(
            ("report" + extension.upper(), "unknown/type", kind) for extension in extensions
        )
        examples.extend(("report.unknown", media_type.upper(), kind) for media_type in media_types)
    examples.extend(
        [
            ("report.md", "application/pdf", "text"),
            ("report.pdf.zip", "application/pdf", "archive"),
            ("README", "application/octet-stream", "other"),
            ("report.未知", "unknown/type", "other"),
        ]
    )
    with sqlite3.connect(":memory:") as conn:
        for name, media_type, expected in examples:
            actual = conn.execute(
                "SELECT " + _KIND_SQL + " FROM (SELECT ? AS name,? AS media_type) a",
                (*_KIND_PARAMS, name, media_type),
            ).fetchone()[0]
            assert actual == artifact_kind(name, media_type) == expected, (name, media_type)


def test_business_categories_merge_text_code_and_documents_and_ignore_mime():
    for kind, extensions, _ in ARTIFACT_KIND_RULES:
        expected = (
            "document"
            if kind in {"document", "text", "code"}
            else kind
            if kind in {"presentation", "spreadsheet", "image"}
            else "other"
        )
        for suffix in extensions:
            assert artifact_business_category("result" + suffix.upper()) == expected
    for name in ("README", "report.unknown", "report.pdf.zip", "config.İNI"):
        assert artifact_business_category(name) == "other"
