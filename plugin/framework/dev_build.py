"""Dev/test build detection shared across framework and UI."""

from __future__ import annotations

import os


def is_dev_build() -> bool:
    """True when ``WRITERAGENT_TESTING`` is exactly ``"1"`` (live env read, not cached)."""
    return os.environ.get("WRITERAGENT_TESTING") == "1"
