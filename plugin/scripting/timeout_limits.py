# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Python execution timeout limits from scripting module.yaml (single source of truth)."""

from __future__ import annotations

from typing import Any

_CONFIG_KEY = "scripting.python_exec_timeout"
_FALLBACK_DEFAULT = 10
_FALLBACK_MIN = 1
_FALLBACK_MAX = 600


def _schema_field() -> dict[str, Any] | None:
    try:
        from plugin._manifest import MODULES
    except ImportError:
        return None
    for m in MODULES:
        if not isinstance(m, dict):
            continue
        if m.get("name") != "scripting":
            continue
        config = m.get("config", {})
        if isinstance(config, dict):
            field = config.get("python_exec_timeout")
            if isinstance(field, dict):
                return field
    return None


def _schema_int(name: str, fallback: int) -> int:
    field = _schema_field()
    if not field:
        return fallback
    val = field.get(name)
    if isinstance(val, int):
        return val
    return fallback


def python_exec_timeout_default() -> int:
    return _schema_int("default", _FALLBACK_DEFAULT)


def python_exec_timeout_min() -> int:
    return _schema_int("min", _FALLBACK_MIN)


def python_exec_timeout_max() -> int:
    return _schema_int("max", _FALLBACK_MAX)


def _clamp_timeout(value: int) -> int:
    lo = python_exec_timeout_min()
    hi = python_exec_timeout_max()
    return max(lo, min(hi, value))


def resolve_python_exec_timeout(
    timeout_sec: int | float | str | None,
    *,
    configured: int | None = None,
) -> int:
    """Clamp *timeout_sec* to schema min/max; invalid values use *configured* or schema default."""
    base = configured if configured is not None else python_exec_timeout_default()
    if timeout_sec is None:
        return _clamp_timeout(base)
    try:
        parsed = int(float(timeout_sec))
    except (TypeError, ValueError):
        return _clamp_timeout(base)
    return _clamp_timeout(parsed)


def configured_python_exec_timeout(ctx: Any) -> int:
    """Read Settings value for scripting.python_exec_timeout and clamp to schema bounds."""
    from plugin.framework.config import get_config_int

    try:
        val = get_config_int(ctx, _CONFIG_KEY)
    except Exception:
        val = python_exec_timeout_default()
    return _clamp_timeout(val)
