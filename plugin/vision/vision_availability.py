# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Gate LLM vision/OCR tools on Settings venv configuration (fast) or package probe (diagnostics)."""

from __future__ import annotations

import copy
import logging
import os
from typing import Any

from plugin.framework.config import get_config_str
from plugin.framework.errors import ConfigError
from plugin.scripting.config_limits import VISION_PROBE_TIMEOUT_SEC
from plugin.scripting.sandbox import resolve_venv_python
from plugin.scripting.venv_diagnostics import probe_vision_packages, vision_ocr_stack_ready

log = logging.getLogger(__name__)

_VISION_DOMAIN = "vision"
_VISION_TOOL_NAME = "extract_structure_from_image"
_DELEGATE_GATEWAY_NAMES = frozenset({"delegate_to_specialized_writer_toolset", "delegate_to_specialized_calc_toolset", "delegate_to_specialized_draw_toolset"})

# Package probe cache (Settings self-check / diagnostics only — not used on Send / get_schemas).
_probe_cache: dict[tuple[str, float], bool] = {}


def invalidate_vision_availability_cache() -> None:
    """Drop cached vision probe results (e.g. after Settings venv path change)."""
    _probe_cache.clear()


def _resolve_vision_python_exe() -> str | None:
    venv_dir = get_config_str("scripting.python_venv_path").strip()
    if not venv_dir:
        return None
    return resolve_venv_python(venv_dir)


def _probe_ready(python_exe: str) -> bool:
    try:
        mtime = os.path.getmtime(python_exe)
    except OSError:
        mtime = 0.0
    cache_key = (python_exe, mtime)
    if cache_key in _probe_cache:
        return _probe_cache[cache_key]

    probe, err = probe_vision_packages(python_exe, timeout=float(VISION_PROBE_TIMEOUT_SEC))
    if err:
        log.debug("vision_packages_probe_ready: probe note: %s", err)
    ready = vision_ocr_stack_ready(probe)
    _probe_cache[cache_key] = ready
    return ready


def specialized_domain_available(domain: str, ctx: Any = None) -> bool:
    """Whether a specialized domain can actually run, for the exposure layers that advertise it.

    Some domains need a backend the install may not have. The discovery catalog has always hidden
    such a domain, but the MCP tool list advertised its tools anyway, so the same install offered
    a capability in one exposure mode and not the other — and a direct_discovery client could not
    reach a tool it had no way to learn about. One rule, consulted by both.

    Unknown domains are available: the default is to advertise, and only a domain with a known
    prerequisite opts into being gated.
    """
    if domain == "vision":
        return vision_venv_configured(ctx)
    return True


def vision_venv_configured(ctx: Any = None) -> bool:
    """True when Settings venv path is set and a python executable resolves (no import probe).

    Used for schema/prompt gating on the main-thread Send path. Missing Docling/Paddle
    packages surface at OCR runtime or via Settings → Python → Test.
    """
    # Config read failures are ConfigError: log them and return False.
    # get_config takes no ctx, so a missing ctx is not a reason to skip Settings.
    try:
        return _resolve_vision_python_exe() is not None
    except ConfigError as exc:
        log.warning("vision_venv_configured: config error: %s", exc)
        return False


def vision_packages_probe_ready(ctx: Any = None) -> bool:
    """True when the venv subprocess probe finds a ready OCR stack (see vision_ocr_stack_ready).

    For Settings diagnostics only — do not call from get_schemas or chat send setup.
    """
    python_exe = _resolve_vision_python_exe()
    if not python_exe:
        return False
    return _probe_ready(python_exe)


def vision_ocr_available(ctx: Any = None) -> bool:
    """Schema/prompt gate: same as :func:`vision_venv_configured` (no subprocess on Send)."""
    return vision_venv_configured(ctx)


def filter_vision_specialized_tools(tools: list[Any], ctx: Any = None) -> list[Any]:
    """Omit extract_structure_from_image when no Settings venv is configured."""
    if vision_venv_configured(ctx):
        return tools
    return [t for t in tools if getattr(t, "name", None) != _VISION_TOOL_NAME]


def filter_vision_delegate_schemas(schemas: list[dict[str, Any]], ctx: Any = None) -> list[dict[str, Any]]:
    """Remove vision from delegate gateway domain enums when no Settings venv is configured."""
    # Apply the same filter when ctx is None. Returning the schemas unchanged
    # advertised vision tools that were not configured.
    if vision_venv_configured(ctx):
        return schemas

    out: list[dict[str, Any]] = []
    for schema in schemas:
        fn = schema.get("function") if isinstance(schema, dict) else None
        if not isinstance(fn, dict) or fn.get("name") not in _DELEGATE_GATEWAY_NAMES:
            out.append(schema)
            continue
        patched = copy.deepcopy(schema)
        props = patched.get("function", {}).get("parameters", {}).get("properties", {})
        domain_prop = props.get("domain") if isinstance(props, dict) else None
        if isinstance(domain_prop, dict) and isinstance(domain_prop.get("enum"), list):
            domain_prop["enum"] = [d for d in domain_prop["enum"] if d != _VISION_DOMAIN]
        out.append(patched)
    return out
