# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Scripting config limits from module.yaml (timeout, max data cells)."""

from __future__ import annotations

from typing import Any

_TIMEOUT_CONFIG_KEY = "scripting.python_exec_timeout"
_TIMEOUT_FALLBACK_DEFAULT = 10
_TIMEOUT_FALLBACK_MIN = 1
_TIMEOUT_FALLBACK_MAX = 600

# Spawn + auto-import prime in PythonWorkerManager._ensure_warmed — not charged against user timeout.
WARM_WORKER_TIMEOUT_SEC = 30

# Single long budget for trusted helpers known to take a long time
# (OCR/layout via vision resolver, spaCy text analytics, SymPy symbolic,
# embeddings, and any future additions in the LONG_TRUSTED_PREFIXES list).
# These bypass the (often small) user-configured python_exec_timeout.
LONG_TRUSTED_WORKER_TIMEOUT_SEC = 300

# Vision-specific execution budgets (used by the vision resolver in client.py).
# The general long trusted list (spaCy, SymPy, vision, etc.) uses LONG_TRUSTED_WORKER_TIMEOUT_SEC.
VISION_WORKER_TIMEOUT_SEC = 120
DOCLING_WORKER_TIMEOUT_SEC = 300


def long_trusted_worker_timeout_sec(_ctx: Any | None = None) -> int:
    """Single long budget for the list of known long-running trusted helpers."""
    del _ctx
    return LONG_TRUSTED_WORKER_TIMEOUT_SEC

# Settings → Python Test: host subprocess import probe (Docling cold import can exceed 5s).
VISION_PROBE_TIMEOUT_SEC = 30

# Settings → Python Test: sentence-transformers cold import can exceed the sandbox budget.
EMBEDDINGS_PROBE_TIMEOUT_SEC = 30
VECTOR_SEARCH_PROBE_TIMEOUT_SEC = 30

_DATA_CELLS_CONFIG_KEY = "scripting.python_max_data_cells"


def _scripting_schema_field(field_name: str, *, required: bool = False) -> dict[str, Any] | None:
    try:
        from plugin._manifest import MODULES
    except ImportError:
        if required:
            raise RuntimeError(
                f"{field_name} missing from manifest; run make manifest "
                "(plugin/scripting/module.yaml must define the field)."
            ) from None
        return None
    for m in MODULES:
        if not isinstance(m, dict):
            continue
        if m.get("name") != "scripting":
            continue
        config = m.get("config", {})
        if isinstance(config, dict):
            field = config.get(field_name)
            if isinstance(field, dict):
                return field
    if required:
        raise RuntimeError(
            f"{field_name} missing from manifest; run make manifest "
            "(plugin/scripting/module.yaml must define the field)."
        )
    return None


def _schema_int(field_name: str, name: str, *, fallback: int | None = None, required: bool = False) -> int:
    field = _scripting_schema_field(field_name, required=required)
    if not field:
        if fallback is not None:
            return fallback
        raise RuntimeError(f"{field_name}.{name} must be int in module.yaml/manifest")
    val = field.get(name)
    if isinstance(val, int):
        return val
    if fallback is not None:
        return fallback
    raise RuntimeError(f"{field_name}.{name} must be int in module.yaml/manifest")


# --- python_exec_timeout ---


def python_exec_timeout_default() -> int:
    return _schema_int("python_exec_timeout", "default", fallback=_TIMEOUT_FALLBACK_DEFAULT)


def python_exec_timeout_min() -> int:
    return _schema_int("python_exec_timeout", "min", fallback=_TIMEOUT_FALLBACK_MIN)


def python_exec_timeout_max() -> int:
    return _schema_int("python_exec_timeout", "max", fallback=_TIMEOUT_FALLBACK_MAX)


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
        val = get_config_int(_TIMEOUT_CONFIG_KEY)
    except Exception:
        val = python_exec_timeout_default()
    return _clamp_timeout(val)


def embeddings_worker_timeout_sec(_ctx: Any | None = None) -> int:
    """Wall-clock budget for trusted embeddings RPC (uses the single long trusted budget)."""
    del _ctx
    return long_trusted_worker_timeout_sec()


# --- python_max_data_cells ---


def python_max_data_cells_default() -> int:
    return _schema_int("python_max_data_cells", "default", required=True)


def python_max_data_cells_min() -> int:
    return _schema_int("python_max_data_cells", "min", required=True)


def python_max_data_cells_max() -> int:
    return _schema_int("python_max_data_cells", "max", required=True)


def _clamp_max_data_cells(value: int) -> int:
    lo = python_max_data_cells_min()
    hi = python_max_data_cells_max()
    return max(lo, min(hi, value))


def configured_python_max_data_cells(ctx: Any) -> int:
    """Read Settings value for scripting.python_max_data_cells and clamp to schema bounds."""
    from plugin.framework.config import get_config_int

    return _clamp_max_data_cells(get_config_int(_DATA_CELLS_CONFIG_KEY))
