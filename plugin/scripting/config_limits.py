# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Scripting config limits from module.yaml (timeout, max data cells)."""

from __future__ import annotations

from typing import Callable

from plugin.framework.deal_shim import deal

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

# Opt-in wall clock for helpers that call long_trusted_worker_timeout_sec():
# SymPy (run_symbolic), spaCy (run_text_analytics), Docling vision, and
# embeddings / langdetect (via embeddings_worker_timeout_sec). There is no
# prefix list. Paddle uses VISION_WORKER_TIMEOUT_SEC. LanguageTool and Vale
# use their own constants below. Bypasses scripting.python_exec_timeout.
LONG_TRUSTED_WORKER_TIMEOUT_SEC = 300

# Paddle budget. Docling uses the long budget above (same 300s as the
# vision.worker_timeout_sec schema default). client._resolve_vision_timeout_sec
# treats that default as the engine budget; any other positive value overrides
# both engines.
VISION_WORKER_TIMEOUT_SEC = 120
DOCLING_WORKER_TIMEOUT_SEC = 300
LANGUAGETOOL_WORKER_TIMEOUT_SEC = 15
VALE_WORKER_TIMEOUT_SEC = 25


def long_trusted_worker_timeout_sec() -> int:
    """Wall-clock seconds for helpers that opt into the long trusted budget."""
    return LONG_TRUSTED_WORKER_TIMEOUT_SEC

# Settings → Python Test: per-package sandbox import probe (independent of scripting.python_exec_timeout).
SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC = 30

# Settings → Python Test: host subprocess import probe (Docling cold import can exceed 5s).
VISION_PROBE_TIMEOUT_SEC = 30

VECTOR_SEARCH_PROBE_TIMEOUT_SEC = 30

_DATA_CELLS_CONFIG_KEY = "scripting.python_max_data_cells"


def _schema_int(field_name: str, name: str, *, fallback: int | None = None, required: bool = False) -> int:
    """One scripting schema field. Fallbacks cover trees without plugin._manifest."""
    from plugin.framework.config_schema import get_config_schema

    field = get_config_schema(f"scripting.{field_name}")
    if field is None:
        if required:
            raise RuntimeError(
                f"scripting.{field_name} missing from module.yaml/manifest; run make manifest"
            )
        assert fallback is not None
        return fallback
    val = field.get(name)
    # bool is an int subclass, so a module.yaml true/false must not pass.
    if type(val) is int:
        return val
    if fallback is not None:
        return fallback
    raise RuntimeError(f"scripting.{field_name}.{name} must be int in module.yaml/manifest")


def _bounds(field_name: str) -> tuple[int, int]:
    """Return (min, max) bounds for a scripting schema field."""
    if field_name == "python_exec_timeout":
        lo = _schema_int(field_name, "min", fallback=_TIMEOUT_FALLBACK_MIN)
        hi = _schema_int(field_name, "max", fallback=_TIMEOUT_FALLBACK_MAX)
    else:
        lo = _schema_int(field_name, "min", required=True)
        hi = _schema_int(field_name, "max", required=True)
    return lo, hi


@deal.ensure(lambda field_name, value, result: isinstance(result, int) and _bounds(field_name)[0] <= result <= _bounds(field_name)[1])
def _clamp(field_name: str, value: int) -> int:
    lo, hi = _bounds(field_name)
    return max(lo, min(hi, value))


# --- python_exec_timeout ---


def python_exec_timeout_default() -> int:
    return _schema_int("python_exec_timeout", "default", fallback=_TIMEOUT_FALLBACK_DEFAULT)


def _timeout_int(value: object, fallback: int) -> int:
    """User/config timeout; anything unparseable uses *fallback*."""
    from plugin.framework.config_schema import parse_int_robust

    try:
        return parse_int_robust(value)
    except (ValueError, TypeError, OverflowError):
        return fallback


@deal.post(lambda result: isinstance(result, int) and _bounds("python_exec_timeout")[0] <= result <= _bounds("python_exec_timeout")[1])
def resolve_python_exec_timeout(
    timeout_sec: object,
    *,
    configured: object = None,
) -> int:
    """Clamp *timeout_sec* to schema min/max; invalid values use *configured* or schema default."""
    base = python_exec_timeout_default()
    if configured is not None:
        base = _timeout_int(configured, base)
    return _clamp("python_exec_timeout", _timeout_int(timeout_sec, base))


def _configured_int(key: str, fallback: Callable[[], int]) -> int:
    """Read one settings int; a failed read uses the schema default, not the clamp floor."""
    from plugin.framework.config import get_config_int

    try:
        return get_config_int(key)
    except Exception:
        return fallback()


def configured_python_exec_timeout() -> int:
    """Read Settings value for scripting.python_exec_timeout and clamp to schema bounds."""
    return _clamp("python_exec_timeout", _configured_int(_TIMEOUT_CONFIG_KEY, python_exec_timeout_default))


def embeddings_worker_timeout_sec() -> int:
    """Wall-clock budget for trusted embeddings RPC (uses the single long trusted budget)."""
    return long_trusted_worker_timeout_sec()


# --- python_max_data_cells ---


def python_max_data_cells_min() -> int:
    return _schema_int("python_max_data_cells", "min", required=True)


def configured_python_max_data_cells() -> int:
    """Read Settings value for scripting.python_max_data_cells and clamp to schema bounds."""
    return _clamp(
        "python_max_data_cells",
        _configured_int(
            _DATA_CELLS_CONFIG_KEY,
            lambda: _schema_int("python_max_data_cells", "default", required=True),
        ),
    )
