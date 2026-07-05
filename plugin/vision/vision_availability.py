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
from plugin.scripting.config_limits import VISION_PROBE_TIMEOUT_SEC
from plugin.scripting.sandbox import resolve_venv_python
from plugin.scripting.venv_diagnostics import _probe_vision_packages

log = logging.getLogger(__name__)

_VISION_DOMAIN = "vision"
_VISION_TOOL_NAME = "extract_text_from_image"
_DELEGATE_GATEWAY_NAMES = frozenset(
    {
        "delegate_to_specialized_writer_toolset",
        "delegate_to_specialized_calc_toolset",
        "delegate_to_specialized_draw_toolset",
    }
)

# Package probe cache (Settings self-check / diagnostics only — not used on Send / get_schemas).
_probe_cache: dict[tuple[str, float], bool] = {}
_cached_venv_path: str | None = None


def invalidate_vision_availability_cache() -> None:
    """Drop cached vision probe results (e.g. after Settings venv path change)."""
    global _cached_venv_path
    _probe_cache.clear()
    _cached_venv_path = None


def _resolve_vision_python_exe(ctx: Any) -> str | None:
    venv_dir = get_config_str("scripting.python_venv_path").strip()
    if not venv_dir:
        return None
    return resolve_venv_python(venv_dir)


def _ocr_backend_ready(probe: dict[str, Any]) -> bool:
    if probe.get("docling") == "present":
        return True
    return probe.get("paddleocr") == "present" and probe.get("paddle") == "present"


def _probe_ready(python_exe: str) -> bool:
    try:
        mtime = os.path.getmtime(python_exe)
    except OSError:
        mtime = 0.0
    cache_key = (python_exe, mtime)
    if cache_key in _probe_cache:
        return _probe_cache[cache_key]

    probe, err = _probe_vision_packages(python_exe, timeout=float(VISION_PROBE_TIMEOUT_SEC))
    if err:
        log.debug("vision_packages_probe_ready: probe note: %s", err)
    ready = _ocr_backend_ready(probe) and probe.get("css_inline") == "present"
    _probe_cache[cache_key] = ready
    return ready


def vision_venv_configured(ctx: Any) -> bool:
    """True when Settings venv path is set and a python executable resolves (no import probe).

    Used for schema/prompt gating on the main-thread Send path. Missing Docling/Paddle
    packages surface at OCR runtime or via Settings → Python → Test.
    """
    if ctx is None:
        return False
    return _resolve_vision_python_exe(ctx) is not None


def vision_packages_probe_ready(ctx: Any) -> bool:
    """True when the venv subprocess probe finds Docling or PaddleOCR+Paddle and css-inline.

    For Settings diagnostics only — do not call from get_schemas or chat send setup.
    """
    global _cached_venv_path
    if ctx is None:
        return False

    venv_dir = get_config_str("scripting.python_venv_path").strip()
    if venv_dir != _cached_venv_path:
        invalidate_vision_availability_cache()
        _cached_venv_path = venv_dir

    python_exe = _resolve_vision_python_exe(ctx)
    if not python_exe:
        return False
    return _probe_ready(python_exe)


def vision_ocr_available(ctx: Any) -> bool:
    """Schema/prompt gate: same as :func:`vision_venv_configured` (no subprocess on Send)."""
    return vision_venv_configured(ctx)


def filter_vision_specialized_tools(tools: list[Any], ctx: Any) -> list[Any]:
    """Omit extract_text_from_image when no Settings venv is configured."""
    if vision_venv_configured(ctx):
        return tools
    return [t for t in tools if getattr(t, "name", None) != _VISION_TOOL_NAME]


def filter_get_image_for_text_only_model(tools: list[Any]) -> list[Any]:
    """Drop get_image when the configured CHAT text model has no native vision.

    get_image only helps a model that can actually SEE the returned image. For the chat (openai)
    path the text model is known, so a text-only model shouldn't be offered it (Keith: every tool
    a small/blind model can't use is wasted context + a chance to mispick). The MCP path does NOT
    call this -- there we assume the connecting client is vision-capable and always expose it.
    Fail OPEN: if the model's vision can't be determined, keep the tool rather than hide a working one."""
    try:
        from plugin.framework.client.model_fetcher import has_native_vision, get_text_model, get_current_endpoint
        if has_native_vision(get_text_model(), get_current_endpoint()):
            return tools
    except Exception:
        return tools
    return [t for t in tools if getattr(t, "name", None) != "get_image"]


def filter_vision_delegate_schemas(schemas: list[dict[str, Any]], ctx: Any) -> list[dict[str, Any]]:
    """Remove vision from delegate gateway domain enums when no Settings venv is configured."""
    if ctx is None or vision_venv_configured(ctx):
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


def vision_domain_hidden(ctx: Any) -> bool:
    """True when the vision specialized domain must not appear in prompts or schemas."""
    return not vision_venv_configured(ctx)
