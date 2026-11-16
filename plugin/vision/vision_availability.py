# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Gate specialized domains (vision/OCR and mcp) on Settings configuration (fast, no package/network probe)."""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.config import get_config
from plugin.framework.constants import DELEGATE_SUBAGENT_TOOL
from plugin.scripting.sandbox import resolve_venv_python

log = logging.getLogger(__name__)

_VISION_TOOL_NAME = "image_extract_structure"


def specialized_domain_available(domain: str, ctx: Any = None) -> bool:
    """Whether a specialized domain can actually run, for the exposure layers that advertise it.

    Some domains need a backend the install may not have. The discovery catalog has always hidden
    such a domain, but the MCP tool list advertised its tools anyway, so the same install offered
    a capability in one exposure mode and not the other — and a direct_discovery client could not
    reach a tool it had no way to learn about. One rule, consulted by both.

    Unknown domains are available: the default is to advertise, and only a domain with a known
    prerequisite opts into being gated (``vision`` requires a Settings venv; ``mcp`` requires at
    least one configured external MCP server).
    """
    if domain == "vision":
        return vision_venv_configured(ctx)
    if domain == "mcp":
        # Lazy: LibrePy ships plugin/vision/ without plugin/mcp/, and only module-level imports are
        # checked by the LibrePy import graph test.
        from plugin.mcp.client import configured_servers

        return bool(configured_servers())
    return True


def vision_venv_configured(ctx: Any = None) -> bool:
    """True when Settings venv path is set and a python executable resolves (no import probe).

    Used for schema/prompt gating on the main-thread Send path. Missing Docling/Paddle
    packages surface at OCR runtime or via Settings → Python → Test.
    """
    venv_dir = str(get_config("scripting.python_venv_path") or "").strip()
    if not venv_dir:
        return False
    return resolve_venv_python(venv_dir) is not None


def filter_vision_specialized_tools(tools: list[Any], ctx: Any = None) -> list[Any]:
    """Omit image_extract_structure when no Settings venv is configured."""
    if vision_venv_configured(ctx):
        return tools
    return [t for t in tools if getattr(t, "name", None) != _VISION_TOOL_NAME]


def filter_unavailable_delegate_domains(schemas: list[dict[str, Any]], ctx: Any = None) -> list[dict[str, Any]]:
    """Remove unavailable domains (such as ``vision`` or ``mcp``) from delegate gateway domain enums."""
    out: list[dict[str, Any]] = []
    for schema in schemas:
        fn = schema.get("function") if isinstance(schema, dict) else None
        if not isinstance(fn, dict) or fn.get("name") != DELEGATE_SUBAGENT_TOOL:
            out.append(schema)
            continue
        params = fn.get("parameters")
        props = params.get("properties") if isinstance(params, dict) else None
        domain_prop = props.get("domain") if isinstance(props, dict) else None
        if not isinstance(params, dict) or not isinstance(props, dict) or not isinstance(domain_prop, dict) or not isinstance(domain_prop.get("enum"), list):
            out.append(schema)
            continue
        enum = domain_prop["enum"]
        kept = [d for d in enum if specialized_domain_available(str(d), ctx)]
        if len(kept) == len(enum):
            out.append(schema)
            continue
        # Copy only the path being changed; deepcopy of the whole schema would run on
        # every get_schemas call while a domain is unconfigured.
        new_domain = {**domain_prop, "enum": kept}
        out.append({**schema, "function": {**fn, "parameters": {**params, "properties": {**props, "domain": new_domain}}}})
    return out


def get_vision_core_directive(model: Any, ctx: Any) -> str:
    """OCR delegation hint for Calc and Draw when the local vision stack is configured.

    Writer carries this routing in its vision catalog line (ToolWriterVisionBase), which
    the domain list shows only when vision is available.
    """
    if ctx is None:
        return ""
    from plugin.framework import doc_type as _doc_type

    if not vision_venv_configured(ctx):
        return ""
    if _doc_type.is_draw(model):
        return (
            'When the user wants OCR or content from an embedded image, '
            f'call {DELEGATE_SUBAGENT_TOOL}(domain="vision"). '
            "That runs OCR on the currently selected image and returns the recognized text "
            "(the slide stays unchanged). You must use this call to perform OCR."
        )
    if not _doc_type.is_calc(model):
        return ""
    return (
        f'When the user wants OCR or content from an embedded image, call {DELEGATE_SUBAGENT_TOOL}(domain="vision"). '
        "That runs OCR on the currently selected image and inserts the recognized text, with its layout and tables, "
        "below the image. You must use this call to perform OCR."
    )
