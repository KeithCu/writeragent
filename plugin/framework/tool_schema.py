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

Registry lookup and ``ToolRegistry.execute`` stay in ``tool``. ``coerce_call_args``
aligns MCP-widened arguments with the source schema. ``call_properties`` /
``without_unknown_kwargs`` are the one allow-list for a call: an empty
``properties`` object is a no-arg schema. ``normalize_outbound_tool_calls``
applies that allow-list once, before any provider shim builds a request.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from plugin.framework.deal_shim import DEAL_MAX_CMD_ARGS, DEAL_MAX_TOKEN, ascii_bounded, deal


_SCALAR_TYPES = frozenset({"integer", "number", "boolean", "string"})


def _schema_type_includes_array(type_value: Any) -> bool:
    """True when a JSON-schema ``type`` is ``array`` or a list that includes it."""
    if type_value == "array":
        return True
    return isinstance(type_value, list) and "array" in type_value


def call_properties(schema: Any) -> dict[str, Any] | None:
    """Properties object used to check a call, including ``{}``.

    What was wrong: ``if props`` treated an empty ``properties`` object as
    "no schema", so hallucinated kwargs reached no-arg tools. A dict schema
    defaults a missing ``properties`` key to ``{}``, and that object is
    closed. A non-dict schema is not an allow-list.
    """
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        return None
    return props


def without_unknown_kwargs(schema: Any, kwargs: dict[str, Any], extra_ok: Any = None) -> dict[str, Any]:
    """Drop keys the schema does not declare.

    Returns *kwargs* unchanged when every key is allowed, so callers can
    keep the original argument text when nothing was hallucinated.
    """
    if not isinstance(kwargs, dict):
        return kwargs
    props = call_properties(schema)
    if props is None:
        return kwargs
    allowed = extra_ok or frozenset()
    if all(key in props or key in allowed for key in kwargs):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in props or key in allowed}


def _parameter_schema(tool: Any) -> tuple[str, dict[str, Any]] | None:
    """OpenAI function wrapper or a flat tool dict → ``(name, parameters)``."""
    if not isinstance(tool, dict):
        return None
    fn = tool.get("function")
    src = fn if isinstance(fn, dict) else tool
    name = src.get("name")
    if not isinstance(name, str) or not name:
        return None
    params = src.get("parameters")
    if not isinstance(params, dict):
        params = src.get("input_schema")
    if not isinstance(params, dict):
        params = src.get("inputSchema")
    if not isinstance(params, dict):
        params = {}
    return name, params


def _merge_parameter_schemas(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Union ``properties`` so a later duplicate name cannot hide the earlier schema.

    What was wrong: last-wins kept only the survivor. An empty ``properties``
    object on the second entry erased the first entry's keys, and a real
    argument then looked unknown. How: keep every property key, first spec
    wins for a repeated key. Why: the call is checked against every schema
    advertised for that name.
    """
    if "properties" not in left and "properties" not in right:
        return left
    merged_props: dict[str, Any] = {}
    for src in (left, right):
        props = src.get("properties")
        if isinstance(props, dict):
            for key, spec in props.items():
                if key not in merged_props:
                    merged_props[key] = spec
    out = dict(left)
    out["properties"] = merged_props
    if "type" not in out:
        right_type = right.get("type")
        out["type"] = right_type if isinstance(right_type, str) else "object"
    return out


def _schemas_by_tool_name(tools: list[Any]) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for tool in tools:
        parsed = _parameter_schema(tool)
        if parsed is None:
            continue
        name, params = parsed
        if name in schemas:
            schemas[name] = _merge_parameter_schemas(schemas[name], params)
        else:
            schemas[name] = params
    return schemas


def _strip_call_arguments(schema: dict[str, Any], arguments: Any) -> Any:
    """Return arguments with unknown keys removed. Unchanged when nothing drops.

    A non-object JSON value is left alone. Bad JSON is left alone so a shim
    can still omit the call instead of sending ``{}``.
    """
    if isinstance(arguments, dict):
        return without_unknown_kwargs(schema, arguments)
    if not isinstance(arguments, str) or not arguments:
        return arguments
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return arguments
    if not isinstance(parsed, dict):
        return arguments
    stripped = without_unknown_kwargs(schema, parsed)
    if stripped is parsed:
        return arguments
    return json.dumps(stripped)


def _normalize_one_outbound_call(call: Any, schemas: dict[str, dict[str, Any]]) -> Any:
    if not isinstance(call, dict):
        return call
    fn = call.get("function")
    if not isinstance(fn, dict):
        return call
    name = fn.get("name")
    if not isinstance(name, str) or name not in schemas:
        return call
    new_args = _strip_call_arguments(schemas[name], fn.get("arguments"))
    if new_args is fn.get("arguments"):
        return call
    new_fn = dict(fn)
    new_fn["arguments"] = new_args
    new_call = dict(call)
    new_call["function"] = new_fn
    return new_call


def normalize_outbound_tool_calls(messages: list[Any], tools: Any) -> list[Any]:
    """Schema-check tool-call arguments before a provider shim sees them.

    One allow-list for every provider. ``properties: {}`` drops hallucinated
    kwargs on a no-arg tool. Duplicate tool names union their properties
    instead of keeping only the last schema.
    """
    if not isinstance(messages, list) or not isinstance(tools, list) or not tools:
        return messages
    schemas = _schemas_by_tool_name(tools)
    if not schemas:
        return messages
    for message in messages:
        if not isinstance(message, dict):
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        replaced: list[Any] | None = None
        for index, call in enumerate(calls):
            updated = _normalize_one_outbound_call(call, schemas)
            if updated is not call:
                if replaced is None:
                    replaced = list(calls)
                replaced[index] = updated
        if replaced is not None:
            message["tool_calls"] = replaced
    return messages


def coerce_call_args(tool_name: str | None, props: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Align a call with the source schema before ``validate``.

    MCP widens array ``range`` to string|array. Several Calc tools index
    ``[0]``, so a bare string would become its first character. Wrap only
    when this property's type includes array. Nested ``range`` fields are
    not walked.

    ``write_formula_range`` values stay type string on the source schema
    (Gemini/Groq must not see a union) while MCP sends a native array.
    A list or number becomes the string ``execute`` already json-encodes.
    """
    if not isinstance(kwargs, dict):
        return kwargs
    out = kwargs
    range_schema = props.get("range") if isinstance(props, dict) else None
    range_type = range_schema.get("type") if isinstance(range_schema, dict) else None
    if isinstance(out.get("range"), str) and _schema_type_includes_array(range_type):
        out = dict(out)
        out["range"] = [out["range"]]
    if tool_name == "write_formula_range":
        fov = out.get("values")
        coerced: str | None = None
        if isinstance(fov, list):
            coerced = json.dumps(fov) if fov else ""
        elif isinstance(fov, (int, float)) and not isinstance(fov, bool):
            coerced = str(fov)
        if coerced is not None:
            if out is kwargs:
                out = dict(out)
            out["values"] = coerced
    return out


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
        # Strict providers apply enum after type; JSON null must be in both.
        # The string "null" is a different value and fails the source enum.
        enum_val = out.get("enum")
        if isinstance(enum_val, list) and None not in enum_val:
            out["enum"] = [*enum_val, None]
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
