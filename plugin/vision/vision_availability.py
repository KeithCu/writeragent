# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Gate LLM vision/OCR tools on Settings venv configuration (fast, no package probe)."""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.config import get_config_str
from plugin.framework.errors import ConfigError
from plugin.scripting.sandbox import resolve_venv_python

log = logging.getLogger(__name__)

_VISION_DOMAIN = "vision"
_VISION_TOOL_NAME = "extract_structure_from_image"
_DELEGATE_GATEWAY_NAMES = frozenset({"delegate_to_specialized_writer_toolset", "delegate_to_specialized_calc_toolset", "delegate_to_specialized_draw_toolset"})


def _resolve_vision_python_exe() -> str | None:
    venv_dir = get_config_str("scripting.python_venv_path").strip()
    if not venv_dir:
        return None
    return resolve_venv_python(venv_dir)


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
        params = fn.get("parameters")
        props = params.get("properties") if isinstance(params, dict) else None
        domain_prop = props.get("domain") if isinstance(props, dict) else None
        if not isinstance(params, dict) or not isinstance(props, dict):
            out.append(schema)
            continue
        if not (isinstance(domain_prop, dict) and isinstance(domain_prop.get("enum"), list)):
            out.append(schema)
            continue
        # Copy only the path being changed; deepcopy of the whole schema ran on
        # every get_schemas call while the venv is unset.
        new_domain = {**domain_prop, "enum": [d for d in domain_prop["enum"] if d != _VISION_DOMAIN]}
        out.append({**schema, "function": {**fn, "parameters": {**params, "properties": {**props, "domain": new_domain}}}})
    return out
