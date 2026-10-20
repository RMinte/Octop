"""Fixed, host-independent browsing categories for published task files."""

from __future__ import annotations

from string import ascii_lowercase, ascii_uppercase
from typing import Literal

ArtifactKind = Literal[
    "document",
    "spreadsheet",
    "presentation",
    "text",
    "code",
    "image",
    "archive",
    "audio",
    "video",
    "other",
]

# Version 1: extensions take precedence over MIME. SQL filters consume this same
# table, so classification stays identical before and after pagination.
ARTIFACT_KIND_RULES: tuple[tuple[ArtifactKind, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "document",
        (".pdf", ".doc", ".docx", ".odt", ".rtf", ".pages"),
        (
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.oasis.opendocument.text",
            "application/rtf",
            "text/rtf",
            "application/vnd.apple.pages",
            "application/x-iwork-pages-sffpages",
        ),
    ),
    (
        "spreadsheet",
        (".csv", ".tsv", ".xls", ".xlsx", ".ods", ".numbers"),
        (
            "text/csv",
            "text/tab-separated-values",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.oasis.opendocument.spreadsheet",
            "application/vnd.apple.numbers",
            "application/x-iwork-numbers-sffnumbers",
        ),
    ),
    (
        "presentation",
        (".ppt", ".pptx", ".odp", ".key"),
        (
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.oasis.opendocument.presentation",
            "application/vnd.apple.keynote",
            "application/x-iwork-keynote-sffkey",
        ),
    ),
    (
        "text",
        (".txt", ".md", ".markdown", ".log", ".rst", ".adoc"),
        ("text/plain", "text/markdown", "text/x-rst", "text/asciidoc"),
    ),
    (
        "code",
        (
            ".py",
            ".pyi",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            ".ts",
            ".tsx",
            ".swift",
            ".json",
            ".jsonl",
            ".ndjson",
            ".yaml",
            ".yml",
            ".html",
            ".htm",
            ".css",
            ".scss",
            ".sass",
            ".less",
            ".xml",
            ".toml",
            ".ini",
            ".cfg",
            ".conf",
            ".sh",
            ".bash",
            ".zsh",
            ".ps1",
            ".bat",
            ".cmd",
            ".sql",
            ".rs",
            ".go",
            ".java",
            ".kt",
            ".kts",
            ".c",
            ".h",
            ".cc",
            ".cpp",
            ".hpp",
            ".cs",
            ".rb",
            ".php",
            ".vue",
            ".svelte",
            ".ipynb",
            ".r",
            ".lua",
            ".dart",
        ),
        (
            "application/json",
            "application/ld+json",
            "application/x-ndjson",
            "application/javascript",
            "text/javascript",
            "text/typescript",
            "text/x-python",
            "text/html",
            "text/css",
            "application/xml",
            "text/xml",
            "application/yaml",
            "application/x-yaml",
            "text/yaml",
            "text/x-yaml",
            "application/toml",
            "application/x-sh",
            "text/x-shellscript",
            "text/x-java-source",
            "application/sql",
        ),
    ),
    (
        "image",
        (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".svg",
            ".heic",
            ".heif",
            ".avif",
            ".bmp",
            ".ico",
            ".tif",
            ".tiff",
        ),
        (
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
            "image/svg+xml",
            "image/heic",
            "image/heif",
            "image/avif",
            "image/bmp",
            "image/x-icon",
            "image/vnd.microsoft.icon",
            "image/tiff",
        ),
    ),
    (
        "archive",
        (".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz", ".zst"),
        (
            "application/zip",
            "application/x-tar",
            "application/gzip",
            "application/x-gzip",
            "application/x-7z-compressed",
            "application/vnd.rar",
            "application/x-rar-compressed",
            "application/x-bzip2",
            "application/x-xz",
            "application/zstd",
        ),
    ),
    (
        "audio",
        (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".aac", ".aiff", ".opus"),
        (
            "audio/mpeg",
            "audio/wav",
            "audio/x-wav",
            "audio/mp4",
            "audio/x-m4a",
            "audio/flac",
            "audio/x-flac",
            "audio/ogg",
            "audio/aac",
            "audio/aiff",
            "audio/x-aiff",
            "audio/opus",
        ),
    ),
    (
        "video",
        (".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi", ".mpeg", ".mpg", ".3gp"),
        (
            "video/mp4",
            "video/quicktime",
            "video/webm",
            "video/x-matroska",
            "video/x-m4v",
            "video/x-msvideo",
            "video/mpeg",
            "video/3gpp",
        ),
    ),
)

_ASCII_LOWER = str.maketrans(ascii_uppercase, ascii_lowercase)


def ascii_lower(value: str) -> str:
    """Fold ASCII letters only; preserve every other character literally."""
    return value.translate(_ASCII_LOWER)


def artifact_kind(name: str, media_type: str) -> ArtifactKind:
    lowered_name = ascii_lower(name)
    for kind, extensions, _ in ARTIFACT_KIND_RULES:
        if lowered_name.endswith(extensions):
            return kind
    lowered_media_type = ascii_lower(media_type)
    for kind, _, media_types in ARTIFACT_KIND_RULES:
        if lowered_media_type in media_types:
            return kind
    return "other"
