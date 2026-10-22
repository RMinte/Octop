"""URL validation for explicitly configured box model services."""

from __future__ import annotations

from urllib.parse import urlsplit


def validate_box_base_url(url: str) -> str:
    """Allow HTTP/HTTPS box hosts, including loopback and LAN addresses."""
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and not any(char in url for char in "?#\\")
            and not parsed.netloc.endswith(":")
            and (parsed.port is None or 0 < parsed.port <= 65535)
            and not any(char.isspace() or ord(char) < 32 for char in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Invalid box model base URL")
    return url.rstrip("/")
