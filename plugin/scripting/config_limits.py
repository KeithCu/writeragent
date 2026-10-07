# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Scripting config limits from module.yaml (timeout, max data cells)."""

from __future__ import annotations

import functools
from typing import Any

from plugin.framework.deal_shim import DEAL_MAX_ARGV, DEAL_MAX_TOKEN, UNDER_CROSSHAIR, str_bounded, deal

_TIMEOUT_CONFIG_KEY = "scripting.python_exec_timeout"
# Fallbacks duplicate module.yaml so an OXT can import without plugin._manifest.
# test_config_limits.test_timeout_fallbacks_match_manifest; do not drop the fallbacks.
_TIMEOUT_FALLBACK_DEFAULT = 10
_TIMEOUT_FALLBACK_MIN = 1
_TIMEOUT_FALLBACK_MAX = 600

# Spawn + auto-import prime in PythonWorkerManager._ensure_warmed — not charged against user timeout.
WARM_WORKER_TIMEOUT_SEC = 30

# Local pipe writes should complete quickly even for large Calc payloads. A separate
# bound prevents a child that stopped reading stdin from holding the pool lock forever.
VENV_IPC_WRITE_TIMEOUT_SEC = 10

# Host-side read grace margin above the child's in-process execution timeout. This ensures the
# child's in-process signal/thread timeout (signal.alarm / ExecutionTimeoutError) fires first and
# returns a clean error payload, preventing the host from terminating the warm subprocess (and
# destroying shared workbook sessions) on soft timeouts.
HOST_IPC_READ_GRACE_SEC = 2.0

# Single long budget for trusted helpers known to take a long time
# (OCR/layout via vision resolver, spaCy text analytics, SymPy symbolic,
# embeddings, and any future additions in the LONG_TRUSTED_PREFIXES list).
# These bypass the (often small) user-configured python_exec_timeout.
LONG_TRUSTED_WORKER_TIMEOUT_SEC = 300

# Vision-specific execution budgets (used by the vision resolver in client.py).
# The general long trusted list (spaCy, SymPy, vision, etc.) uses LONG_TRUSTED_WORKER_TIMEOUT_SEC.
VISION_WORKER_TIMEOUT_SEC = 120
DOCLING_WORKER_TIMEOUT_SEC = 300
LANGUAGETOOL_WORKER_TIMEOUT_SEC = 15
VALE_WORKER_TIMEOUT_SEC = 25


def long_trusted_worker_timeout_sec() -> int:
    """Single long budget for the list of known long-running trusted helpers."""
    return LONG_TRUSTED_WORKER_TIMEOUT_SEC

# Settings → Python Test: per-package sandbox import probe (independent of scripting.python_exec_timeout).
SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC = 30

# Settings → Python Test: host subprocess import probe (Docling cold import can exceed 5s).
VISION_PROBE_TIMEOUT_SEC = 30

# Settings → Python Test: sentence-transformers cold import can exceed the sandbox budget.
EMBEDDINGS_PROBE_TIMEOUT_SEC = 30
VECTOR_SEARCH_PROBE_TIMEOUT_SEC = 30

_DATA_CELLS_CONFIG_KEY = "scripting.python_max_data_cells"


@functools.cache
def _scripting_schema_field(field_name: str, *, required: bool = False) -> dict[str, Any] | None:
    try:
        from plugin._manifest import MODULES
    except ImportError:
        MODULES = []
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


@functools.cache
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


# bool is an int subclass. ``isinstance(x, bool) is False`` uses object identity,
# so CrossHair's SymbolicBool never matches False and the pre never holds for
# symbolic ints (check-all deep 32900105768: resolve(..., configured=33) then
# nested _clamp_timeout). ``type(x) is int`` rejects bools the same way for
# real values and is CrossHair-friendly.
def _timeout_int_ok_pytest(value: object) -> bool:
    # A hand-edited timeout past DEAL_MAX_ARGV (86400 is one day; the schema
    # max is 600) raised PreContractError before _clamp_timeout could clamp.
    # bool stays out: type(True) is not int. CrossHair keeps the abs cap.
    return type(value) is int


def _timeout_int_ok_crosshair(value: object) -> bool:
    return type(value) is int and abs(value) <= DEAL_MAX_ARGV


_timeout_int_ok = _timeout_int_ok_crosshair if UNDER_CROSSHAIR else _timeout_int_ok_pytest


def _timeout_sec_ok(timeout_sec: object) -> bool:
    """Domain for resolve_python_exec_timeout that cannot nested-fail _clamp_timeout.

    Numeric strings take the parse → _clamp_timeout path. Length-only
    ``str_bounded`` still allowed CrossHair's ``'100'`` (DEAL_MAX_ARGV=32)
    after the bool-identity fix.
    """
    if timeout_sec is None:
        return True
    if _timeout_int_ok(timeout_sec):
        return True
    if isinstance(timeout_sec, float) and abs(timeout_sec) <= DEAL_MAX_ARGV:
        return True
    if isinstance(timeout_sec, str) and str_bounded(timeout_sec, DEAL_MAX_TOKEN):
        try:
            parsed = int(float(timeout_sec))
        except (TypeError, ValueError, OverflowError):
            return True
        return _timeout_int_ok(parsed)
    return False


def _timeout_configured_ok(configured: object) -> bool:
    return configured is None or _timeout_int_ok(configured)


@deal.pre(lambda value: _timeout_int_ok(value))
@deal.post(lambda result: isinstance(result, int) and python_exec_timeout_min() <= result <= python_exec_timeout_max())
def _clamp_timeout(value: int) -> int:
    lo = python_exec_timeout_min()
    hi = python_exec_timeout_max()
    return max(lo, min(hi, value))


@deal.pre(lambda timeout_sec, configured=None: _timeout_sec_ok(timeout_sec))
# Without this, a real bool (int subclass) or an int outside DEAL_MAX_ARGV leaks
# into _clamp_timeout and CrossHair reports a nested PreconditionFailed.
@deal.pre(lambda timeout_sec, configured=None: _timeout_configured_ok(configured))
@deal.post(lambda result: isinstance(result, int) and python_exec_timeout_min() <= result <= python_exec_timeout_max())
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
    except (TypeError, ValueError, OverflowError):
        return _clamp_timeout(base)
    return _clamp_timeout(parsed)


# get_config_int_safe reads from global config storage and does not take a UNO
# context. Dropping ctx removes misleading parameter coupling across callers.
def configured_python_exec_timeout() -> int:
    """Read Settings value for scripting.python_exec_timeout and clamp to schema bounds."""
    from plugin.framework.config import get_config_int_safe

    return _clamp_timeout(get_config_int_safe(_TIMEOUT_CONFIG_KEY))


def embeddings_worker_timeout_sec() -> int:
    """Wall-clock budget for trusted embeddings RPC (uses the single long trusted budget)."""
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


def configured_python_max_data_cells() -> int:
    """Read Settings value for scripting.python_max_data_cells and clamp to schema bounds."""
    from plugin.framework.config import get_config_int_safe

    return _clamp_max_data_cells(get_config_int_safe(_DATA_CELLS_CONFIG_KEY))
