# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Core tool schemas for string eval — same ``get_schemas`` path as sidebar chat.

Headless ``ToolRegistry`` + module ``initialize`` (no ``plugin.main.bootstrap``).
"""
from __future__ import annotations

import copy
from typing import Any
from unittest.mock import MagicMock

from plugin.calc import CalcModule
from plugin.chatbot import ChatbotModule
from plugin.draw import DrawModule
from plugin.framework.config import init_config
from plugin.framework.service import ServiceRegistry
from plugin.framework.tool import ToolRegistry
from plugin.writer import WriterModule

_registry: ToolRegistry | None = None


def _headless_registry() -> ToolRegistry:
    """Writer + Calc + Draw + chatbot core tools, filtered later by doc_type."""
    global _registry
    if _registry is not None:
        return _registry

    init_config(MagicMock())
    services = ServiceRegistry()
    services.register("config", MagicMock())
    services.register("document", MagicMock())
    services.register("events", MagicMock())
    tools = ToolRegistry(services)
    services.register("tools", tools)

    WriterModule().initialize(services)
    CalcModule().initialize(services)
    DrawModule().initialize(services)
    ChatbotModule().initialize(services)
    tools.auto_discover_package("plugin.doc")

    _registry = tools
    return tools


def build_eval_tool_schemas(
    *, kind: str, active_domain: str | None = None
) -> list[dict[str, Any]]:
    """OpenAI function schemas for writer / draw / calc — same filter as sidebar.

    ``active_domain`` matches production specialized mode (shapes, ranges, …).
    """
    doc_type = kind if kind in ("writer", "draw", "calc") else "writer"
    kwargs: dict[str, Any] = {
        "doc_type": doc_type,
        "filter_doc_type": True,
    }
    if active_domain:
        kwargs["active_domain"] = active_domain
        kwargs["exclude_tiers"] = ()
    return _headless_registry().get_schemas("openai", **kwargs)


def _schema_function(row: dict[str, Any]) -> dict[str, Any] | None:
    """OpenAI ``{type, function}`` or a bare function dict."""
    fn = row.get("function")
    if isinstance(fn, dict):
        return fn
    if isinstance(row.get("name"), str):
        return row
    return None


def apply_schema_patches(
    schemas: list[dict[str, Any]],
    patches: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return a copy of ``schemas`` with named tool description / param text replaced.

    ``patches`` keys are tool names. Each value may include ``description``
    (tool-level) and/or ``parameters`` (``{param_name: description}``).
    Unknown tools or params are ignored so a Calc-only patch is a no-op on
    Writer catalogs. Always copies: ``get_schemas`` may reuse nested dicts.
    """
    if not patches:
        return schemas
    out = copy.deepcopy(schemas)
    for row in out:
        fn = _schema_function(row)
        if fn is None:
            continue
        name = str(fn.get("name") or "")
        patch = patches.get(name)
        if not isinstance(patch, dict):
            continue
        desc = patch.get("description")
        if isinstance(desc, str):
            fn["description"] = desc
        param_descs = patch.get("parameters")
        if not isinstance(param_descs, dict):
            continue
        props = (fn.get("parameters") or {}).get("properties")
        if not isinstance(props, dict):
            continue
        for pname, pdesc in param_descs.items():
            if pname in props and isinstance(pdesc, str) and isinstance(props[pname], dict):
                props[pname]["description"] = pdesc
    return out
