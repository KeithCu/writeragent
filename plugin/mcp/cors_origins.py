# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""CORS allowed-origin list: normalize, Settings UI merge, runtime cache."""

from __future__ import annotations

import logging

log = logging.getLogger("writeragent.mcp.cors")

MCP_CORS_ORIGINS_KEY = "mcp.cors_allowed_origins"
MCP_CORS_UI_FIELD = "mcp__cors_allowed_origin"

_extra_allowed_origins: frozenset[str] = frozenset()


def normalize_cors_origin(value: str | None) -> str | None:
    """Return a canonical origin URL or None if empty/invalid."""
    if value is None:
        return None
    origin = str(value).strip()
    if not origin:
        return None
    if origin.endswith("/"):
        origin = origin.rstrip("/")
    lower = origin.lower()
    if not (lower.startswith("http://") or lower.startswith("https://")):
        return None
    return origin


def normalize_origins_list(value) -> list[str]:
    """Coerce config value to a deduped list of normalized origin strings."""
    if value is None:
        return []
    if isinstance(value, str):
        one = normalize_cors_origin(value)
        return [one] if one else []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        origin = normalize_cors_origin(item)
        if origin and origin not in out:
            out.append(origin)
    return out


def merge_ui_origin_into_list(origins: list[str], ui_value: str | None) -> list[str]:
    """Settings UI edits index 0 only; preserve origins[1:]."""
    normalized = normalize_origins_list(origins)
    tail = normalized[1:] if normalized else []
    first = normalize_cors_origin(ui_value)
    if first:
        return [first, *tail]
    return tail


def first_origin_for_ui(origins: list[str]) -> str:
    normalized = normalize_origins_list(origins)
    return normalized[0] if normalized else ""


def set_extra_allowed_origins(origins) -> None:
    """Update the in-process cache used by is_safe_origin (HTTP threads, no ctx)."""
    global _extra_allowed_origins
    _extra_allowed_origins = frozenset(normalize_origins_list(origins))


def get_extra_allowed_origins() -> frozenset[str]:
    return _extra_allowed_origins


def is_extra_allowed_origin(origin: str) -> bool:
    if not origin:
        return False
    normalized = normalize_cors_origin(origin)
    return bool(normalized and normalized in _extra_allowed_origins)


def reload_extra_allowed_origins_from_config(services) -> None:
    """Read mcp.cors_allowed_origins from the config service and refresh the cache."""
    try:
        cfg = services.config.proxy_for("mcp")
        raw = cfg.get("cors_allowed_origins")
    except Exception as e:
        log.warning("Could not load cors_allowed_origins: %s", e)
        raw = []
    origins = normalize_origins_list(raw)
    set_extra_allowed_origins(origins)
    if origins:
        log.info("MCP CORS extra allowed origins: %s", ", ".join(origins))
