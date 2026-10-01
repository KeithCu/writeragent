# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2025-2026 quazardous (config, registries, build system)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""OpenAI and MCP JSON Schema conversion for ToolBase instances.

Registry lookup and ``ToolRegistry.execute`` stay in ``tool``. That path
wraps a string ``range`` as ``[str]`` before validation; these converters
only advertise schemas.
"""

from __future__ import annotations

import copy
from typing import Any

from plugin.framework.deal_shim import DEAL_MAX_CMD_ARGS, DEAL_MAX_TOKEN, ascii_bounded, deal


_SCALAR_TYPES = frozenset({"integer", "number", "boolean", "string"})


@deal.pre(lambda types: isinstance(types, list))
@deal.post(lambda result: isinstance(result, (str, list)))
def _collapse_union_type(types: list[str]) -> str | list[str]:
    """Collapse messy unions for Gemini; preserve scalar+null pairs for Groq."""
    # crosshair: off
    if not types:
        return "string"
    non_null = [t for t in types if t != "null"]
    if len(non_null) == 1 and len(types) == 2 and "null" in types:
        return [non_null[0], "null"]
    if "array" in types:
        return "array"
    return non_null[0] if non_null else "string"


@deal.pre(lambda type_val: type_val is None or isinstance(type_val, str) or (isinstance(type_val, list) and len(type_val) <= DEAL_MAX_CMD_ARGS and all(isinstance(x, str) and ascii_bounded(x, DEAL_MAX_TOKEN) for x in type_val)))
def _type_allows_null(type_val: Any) -> bool:
    return isinstance(type_val, list) and "null" in type_val


@deal.ensure(lambda prop_schema, result: not isinstance(prop_schema, dict) or isinstance(result, dict))
def _make_optional_scalar_nullable(prop_schema: dict[str, Any]) -> dict[str, Any]:
    """Add null to optional scalar property types (strict providers reject bare null otherwise)."""
    # crosshair: off
    if not isinstance(prop_schema, dict):
        return prop_schema
    type_val = prop_schema.get("type")
    if type_val is None or _type_allows_null(type_val):
        return prop_schema
    if isinstance(type_val, str) and type_val in _SCALAR_TYPES:
        out = copy.deepcopy(prop_schema)
        out["type"] = [type_val, "null"]
        # Strict providers apply enum after type; null must be in both.
        enum_val = out.get("enum")
        if isinstance(enum_val, list) and "null" not in enum_val:
            out["enum"] = [*enum_val, "null"]
        return out
    return prop_schema


@deal.ensure(lambda params, result: (not isinstance(params, dict) or not params) or isinstance(result, dict))
@deal.ensure(lambda params, result: not isinstance(result, dict) or result.get("required") != [])
def _normalize_schema_for_strict_providers(params: Any) -> Any:
    """Normalize JSON Schema for strict upstream validators (Gemini, Groq, etc.).

    - Optional scalar properties get ``type: [scalar, "null"]`` so models may pass ``null``.
    - ``[scalar, "null"]`` unions are preserved; other unions collapse (e.g. string+array → array).
    - Empty ``required`` is removed so providers do not complain about required[0/1] missing.
    - Nested object properties are normalized recursively with each object's ``required`` list.
    """
    # crosshair: off
    if type(params) is not dict:
        return params
    if len(params) == 0:
        return params
    params = copy.deepcopy(params)
    if "type" in params and isinstance(params["type"], list):
        params["type"] = _collapse_union_type(params["type"])
    if params.get("type") != "array":
        params.pop("items", None)
    if params.get("required") == []:
        params.pop("required", None)

    required_keys = set(params.get("required") or [])

    if params.get("type") == "object" and isinstance(params.get("properties"), dict):
        new_props = {}
        for k, v in params["properties"].items():
            v = _normalize_schema_for_strict_providers(v)
            if k not in required_keys:
                v = _make_optional_scalar_nullable(v)
            new_props[k] = v
        params["properties"] = new_props
    elif "items" in params:
        if isinstance(params["items"], dict):
            params["items"] = _normalize_schema_for_strict_providers(params["items"])
        elif isinstance(params["items"], list) and params["items"]:
            params["items"] = _normalize_schema_for_strict_providers(params["items"][0])
    return params


@deal.pre(lambda tool, **kwargs: getattr(tool, "name", None) is not None)
@deal.post(lambda result: isinstance(result, dict) and result.get("type") == "function" and isinstance(result.get("function"), dict))
@deal.ensure(lambda tool, doc_type=None, result=None, **kwargs: result is not None and isinstance(result, dict) and result.get("function", {}).get("name") == tool.name)
def to_openai_schema(tool: Any, *, doc_type: str | None = None) -> dict[str, Any]:
    """Convert a ToolBase instance to an OpenAI function-calling schema.

    Returns::

        {
            "type": "function",
            "function": {
                "name": "get_document_tree",
                "description": "...",
                "parameters": { ... JSON Schema ... }
            }
        }
    """
    # Host Tool + deepcopy of nested JSON Schema; pytest still checks @deal.
    # crosshair: off
    params = copy.deepcopy(tool.get_parameters(doc_type) or {})
    if "type" not in params:
        params["type"] = "object"
    params = _normalize_schema_for_strict_providers(params)
    desc = tool.get_description(doc_type)

    return {"type": "function", "function": {"name": tool.name, "description": desc, "parameters": params}}


@deal.pre(lambda tool, **kwargs: getattr(tool, "name", None) is not None)
@deal.post(lambda result: isinstance(result, dict) and "inputSchema" in result and result.get("name") is not None)
@deal.ensure(lambda tool, doc_type=None, result=None, **kwargs: result is not None and isinstance(result, dict) and result.get("name") == tool.name)
def to_mcp_schema(tool: Any, *, doc_type: str | None = None) -> dict[str, Any]:
    """Convert a ToolBase instance to an MCP tools/list schema.

    Returns::

        {
            "name": "get_document_outline",
            "description": "...",
            "inputSchema": { ... JSON Schema ... }
        }
    """
    # Host Tool + deepcopy of nested JSON Schema; pytest still checks @deal.
    # crosshair: off
    input_schema = copy.deepcopy(tool.get_parameters(doc_type) or {})
    if "type" not in input_schema:
        input_schema["type"] = "object"
    if "properties" not in input_schema:
        input_schema["properties"] = {}
    if "document_url" not in input_schema["properties"]:
        input_schema["properties"]["document_url"] = {
            "type": "string",
            "description": "Optional URL or RuntimeUID of the target document (both come from list_open_documents). If not provided, the active document is used. A RuntimeUID also targets unsaved/untitled documents that have no file URL yet.",
        }
    desc = tool.get_description(doc_type)

    agent_label = getattr(tool, "_agent_label", None)
    special_base = getattr(tool, "_special_base_class", None)
    if agent_label and special_base is not None:
        from plugin.framework.prompts import format_specialized_domains_description

        # For MCP schemas, use a compact description to avoid duplicating the long domain list
        # (the detailed domain guidance lives in the 'domain' property description instead).
        # The full verbose guidance with examples is still used in chat system prompts.
        desc = (f"{desc} Delegates to a specialized {agent_label} task. See the 'domain' property for available areas and the 'task' parameter rules.").strip()

        props = input_schema.get("properties")
        if isinstance(props, dict) and "domain" in props and isinstance(props["domain"], dict):
            props["domain"]["description"] = format_specialized_domains_description(special_base, agent_label=agent_label)

    input_schema = _normalize_schema_for_strict_providers(input_schema)
    # MCP hosts validate args against inputSchema before tools/call. Keep string|array for
    # write_formula_range so native JSON arrays are accepted (OpenAI/Gemini stay string-only
    # via to_openai_schema collapse — see docs/calc/date-time-handling.md §4.3).
    props = input_schema.get("properties")
    if isinstance(props, dict):
        if tool.name == "write_formula_range" and "values" in props:
            fov = props["values"]
            if isinstance(fov, dict):
                fov = dict(fov)
                fov["type"] = ["string", "array"]
                fov["items"] = {"type": ["string", "number"]}
                desc_bits = fov.get("description") or ""
                if "Native JSON array" not in desc_bits:
                    fov["description"] = ((desc_bits + " " if desc_bits else "") + "Native JSON array of strings/numbers is accepted (same length as the range); a single string still fills the entire range.").strip()
                props["values"] = fov
        # Execute already coerces a bare range string to [str]. Source schemas stay
        # array-only so Gemini/Groq do not see a string|array union (collapse prefers array).
        rn = props.get("range")
        if isinstance(rn, dict) and rn.get("type") == "array":
            rn = dict(rn)
            rn["type"] = ["string", "array"]
            props["range"] = rn
    return {"name": tool.name, "description": desc, "inputSchema": input_schema}
