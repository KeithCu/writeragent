# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Core tool schemas for string eval — same ``get_schemas`` path as sidebar chat.

Headless ``ToolRegistry`` + module ``initialize`` (no ``plugin.main.bootstrap``).

Eval-only transforms (name allowlist, named presets, skinny descriptions)
live here so ``run_eval`` / ``run_eval_multi`` can sweep tool-count and
schema density without touching production sidebar registration.
"""
from __future__ import annotations

import argparse
import copy
from typing import Any, Literal, Sequence
from unittest.mock import MagicMock

from plugin.calc import CalcModule
from plugin.chatbot import ChatbotModule
from plugin.draw import DrawModule
from plugin.framework.config import init_config
from plugin.framework.service import ServiceRegistry
from plugin.framework.tool import ToolRegistry
from plugin.writer import WriterModule

_registry: ToolRegistry | None = None

# Named outer-catalog presets for tool-count dropper sweeps. ``kind`` is the
# ``task_kind`` these names belong to; other kinds keep the full catalog so a
# mixed 17-task run with ``--tools calc_minimal`` does not fail on Writer.
# Specialized inner-loop schemas are never filtered by these lists.
# Outer names are production ``get_schemas`` names (2026-09 dump).
SchemaDensity = Literal["full", "skinny"]
SCHEMA_DENSITIES: tuple[str, ...] = ("full", "skinny")

TOOL_PRESETS: dict[str, tuple[str | None, tuple[str, ...] | None]] = {
    "full": (None, None),
    # Smallest outer set that can still pass data_sorting + tax_column:
    # tax writes via write_formula_range / get_sheet_summary; sort delegates.
    "calc_minimal": (
        "calc",
        (
            "write_formula_range",
            "get_sheet_summary",
            "delegate_to_specialized_calc_toolset",
        ),
    ),
    # Calc-specific outer tools only (drops shared chatbot extras:
    # web_research, upsert_memory, get_guidance, redo, undo).
    "calc_core": (
        "calc",
        (
            "delete_structure",
            "insert_cell_html",
            "merge_cells",
            "read_cell_range",
            "set_style",
            "write_formula_range",
            "list_calc_functions",
            "get_sheet_summary",
            "delegate_to_specialized_calc_toolset",
        ),
    ),
    # Smallest outer set for Writer apply-HTML tasks (table_from_mess, …).
    "writer_minimal": (
        "writer",
        (
            "apply_document_content",
            "get_document_content",
        ),
    ),
}


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


def schema_tool_name(row: dict[str, Any]) -> str:
    """Tool name from an OpenAI ``{type, function}`` row or a bare function dict."""
    fn = _schema_function(row)
    if fn is None:
        return ""
    return str(fn.get("name") or "")


def list_tool_preset_names() -> tuple[str, ...]:
    return tuple(TOOL_PRESETS)


def parse_tools_spec(raw: str | None) -> tuple[str | None, list[str] | None]:
    """Parse ``--tools`` into ``(preset_name, explicit_names)``.

    ``("full", None)`` or ``(None, None)`` means no filter. A known preset
    returns ``(name, None)``. Anything else is a comma-separated allowlist.
    """
    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text or text == "full":
        return "full", None
    if text in TOOL_PRESETS:
        return text, None
    names = [part.strip() for part in text.split(",") if part.strip()]
    if not names:
        return "full", None
    return None, names


def resolve_tool_allowlist(
    spec: str | None,
    *,
    kind: str,
    active_domain: str | None = None,
) -> list[str] | None:
    """Allowlist for this catalog, or ``None`` to keep every advertised tool.

    Specialized inner catalogs (``active_domain`` set) are never filtered so
    ``delegate_to_specialized_*`` still sees ``sort_range`` / shapes / etc.
    Kind-specific presets no-op on other document kinds.
    """
    if active_domain:
        return None
    preset, explicit = parse_tools_spec(spec)
    if explicit is not None:
        return explicit
    if preset is None or preset == "full":
        return None
    pkind, names = TOOL_PRESETS[preset]
    if names is None:
        return None
    if pkind and pkind != kind:
        return None
    return list(names)


def filter_eval_tool_schemas(
    schemas: list[dict[str, Any]],
    names: Sequence[str],
) -> list[dict[str, Any]]:
    """Keep ``schemas`` whose names are in ``names``, in allowlist order.

    Unknown names raise ``ValueError`` with the available catalog names.
    Duplicate allowlist entries are kept once (first occurrence).
    """
    if not names:
        raise ValueError("Tool allowlist is empty.")
    by_name: dict[str, dict[str, Any]] = {}
    available: list[str] = []
    for row in schemas:
        name = schema_tool_name(row)
        if not name or name in by_name:
            continue
        by_name[name] = row
        available.append(name)
    wanted: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = str(name or "").strip()
        if not key or key in seen:
            continue
        wanted.append(key)
        seen.add(key)
    if not wanted:
        raise ValueError("Tool allowlist is empty.")
    unknown = [name for name in wanted if name not in by_name]
    if unknown:
        raise ValueError(
            "Unknown eval tool name(s): "
            + ", ".join(unknown)
            + ". Available: "
            + ", ".join(available)
        )
    return [by_name[name] for name in wanted]


def _blank_schema_descriptions(obj: Any) -> None:
    """Clear every string ``description`` field in a schema tree (in place)."""
    if isinstance(obj, dict):
        desc = obj.get("description")
        if isinstance(desc, str):
            obj["description"] = ""
        for value in obj.values():
            _blank_schema_descriptions(value)
    elif isinstance(obj, list):
        for item in obj:
            _blank_schema_descriptions(item)


def apply_schema_density(
    schemas: list[dict[str, Any]],
    density: SchemaDensity | str | None,
) -> list[dict[str, Any]]:
    """``full`` is a no-op. ``skinny`` keeps names/types, blanks descriptions.

    Fat tool/param prose is the variable; required param names and JSON
    types stay so the model can still call tools. Always copies when
    rewriting so the live registry cache is not mutated.
    """
    mode = (density or "full").strip() or "full"
    if mode == "full":
        return schemas
    if mode != "skinny":
        raise ValueError(
            f"Unknown schema density {mode!r}. Expected one of: "
            + ", ".join(SCHEMA_DENSITIES)
        )
    out = copy.deepcopy(schemas)
    for row in out:
        fn = _schema_function(row)
        if fn is None:
            continue
        _blank_schema_descriptions(fn)
    return out


def prepare_eval_tool_schemas(
    *,
    kind: str,
    active_domain: str | None = None,
    tools_spec: str | None = None,
    schema_density: SchemaDensity | str = "full",
    schema_patches: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build, optionally filter, skinny-transform, then apply MIPRO patches.

    Order is filter → density → patches so a named-slice description still
    lands on a skinny catalog. Name filter applies to the outer catalog only.
    """
    tools = build_eval_tool_schemas(kind=kind, active_domain=active_domain)
    allowlist = resolve_tool_allowlist(
        tools_spec, kind=kind, active_domain=active_domain
    )
    if allowlist is not None:
        tools = filter_eval_tool_schemas(tools, allowlist)
    tools = apply_schema_density(tools, schema_density)
    return apply_schema_patches(tools, schema_patches)


def add_eval_tool_sweep_arguments(parser: argparse.ArgumentParser) -> None:
    """``--tools`` and ``--schema-density`` for the live string harness CLIs."""
    preset_names = ", ".join(list_tool_preset_names())
    parser.add_argument(
        "--tools",
        default="full",
        metavar="SPEC",
        help=(
            f"Outer tool catalog: named preset ({preset_names}) or "
            "comma-separated production tool names. Default: full (no filter). "
            "Kind-specific presets leave other document kinds unchanged. "
            "Does not filter specialized inner-loop schemas."
        ),
    )
    parser.add_argument(
        "--schema-density",
        choices=SCHEMA_DENSITIES,
        default="full",
        help=(
            "full (default): production tool/param descriptions. "
            "skinny: keep names and types, blank descriptions."
        ),
    )
