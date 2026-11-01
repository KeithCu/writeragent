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
from collections.abc import Sequence
from typing import Any, TypeVar

from plugin.framework.deal_shim import deal


_SCALAR_TYPES = frozenset({"integer", "number", "boolean", "string"})
_OutboundMessages = TypeVar("_OutboundMessages", bound=Sequence[Any])


def _schema_type_includes_array(type_value: Any) -> bool:
    """True when a JSON-schema ``type`` is ``array`` or a list that includes it."""
    if type_value == "array":
        return True
    return isinstance(type_value, list) and "array" in type_value


def call_properties(schema: Any) -> dict[str, Any] | None:
    """Properties object used to check a call, including ``{}``.

    An empty ``properties`` object is a closed schema, not "no schema".
    ``if props`` treats it as missing, so hallucinated kwargs reach no-arg
    tools. A dict schema defaults a missing ``properties`` key to ``{}``.
    A non-dict schema is not an allow-list.
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
    params: dict[str, Any] = next(
        (src[key] for key in ("parameters", "input_schema", "inputSchema") if isinstance(src.get(key), dict)),
        {},
    )
    return name, params


def _schemas_by_tool_name(tools: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """Union ``properties`` per name. First spec wins on a repeated key.

    A later empty ``properties`` object must not erase an earlier schema.
    Only properties are stored: ``call_properties`` is the allow-list.
    """
    schemas: dict[str, dict[str, Any]] = {}
    for tool in tools:
        parsed = _parameter_schema(tool)
        if parsed is None:
            continue
        name, params = parsed
        bucket = schemas.setdefault(name, {"properties": {}})
        incoming = params.get("properties")
        if isinstance(incoming, dict):
            props = bucket["properties"]
            for key, spec in incoming.items():
                props.setdefault(key, spec)
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


def _normalize_one_outbound_call(call: Any, schemas: dict[str, dict[str, Any]]) -> None:
    if not isinstance(call, dict):
        return
    fn = call.get("function")
    if not isinstance(fn, dict):
        return
    name = fn.get("name")
    if not isinstance(name, str) or name not in schemas:
        return
    new_args = _strip_call_arguments(schemas[name], fn.get("arguments"))
    if new_args is not fn.get("arguments"):
        fn["arguments"] = new_args


def normalize_outbound_tool_calls(messages: _OutboundMessages, tools: Any) -> _OutboundMessages:
    """Schema-check tool-call arguments before a provider shim sees them.

    One allow-list for every provider. ``properties: {}`` drops hallucinated
    kwargs on a no-arg tool. Duplicate tool names union their properties
    instead of keeping only the last schema.

    *messages* and *tools* may be any sequence. A tuple used to fail the
    ``list`` check and skip the allow-list. Strings are not sequences of
    messages or tools.

    This writes ``function["arguments"]`` on the existing call dicts and
    does not replace ``tool_calls``. ``prepare_chat_messages`` deep-copies
    each message dict before ``make_chat_request`` calls this, so the chat
    path is already a copy and this helper does not copy again. A caller
    that skips ``prepare_chat_messages`` must pass its own copy when it
    still needs the original ``tool_calls``.
    """
    if not tools:
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
        for call in calls:
            _normalize_one_outbound_call(call, schemas)
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
    out = dict(kwargs)
    range_schema = props.get("range")
    range_type = range_schema.get("type") if isinstance(range_schema, dict) else None
    if isinstance(out.get("range"), str) and _schema_type_includes_array(range_type):
        out["range"] = [out["range"]]
    for k, v in out.items():
        if isinstance(v, float) and v.is_integer():
            prop = props.get(k)
            if isinstance(prop, dict):
                t = prop.get("type")
                # _tool_arg_matches_type rejects 3.0 for "integer", so models sending
                # whole floats get VALIDATION_ERROR. 3.5 must still fail.
                if t == "integer" or (isinstance(t, list) and "integer" in t):
                    out[k] = int(v)

    if tool_name == "write_formula_range":
        fov = out.get("values")
        coerced: str | None = None
        if isinstance(fov, list):
            coerced = json.dumps(fov) if fov else ""
        elif isinstance(fov, (int, float)) and not isinstance(fov, bool):
            coerced = str(fov)
        if coerced is not None:
            out["values"] = coerced
    return out


@deal.ensure(lambda prop_schema, result: not isinstance(prop_schema, dict) or isinstance(result, dict))
def _make_optional_scalar_nullable(prop_schema: dict[str, Any]) -> dict[str, Any]:
    """Add null to optional scalar property types (strict providers reject bare null otherwise).

    A union of scalars is optional in the same way as one scalar. Null is
    appended; the other members stay. ``["string", "number"]`` must not
    become ``["string", "null"]``.
    """
    # crosshair: off
    if not isinstance(prop_schema, dict):
        return prop_schema
    raw = prop_schema.get("type")
    types = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else None
    if not types or "null" in types or any(not isinstance(one, str) or one not in _SCALAR_TYPES for one in types):
        return prop_schema
    out = copy.deepcopy(prop_schema)
    out["type"] = [*types, "null"]
    # Strict providers apply enum after type; JSON null must be in both.
    # The string "null" is a different value and fails the source enum.
    enum_val = out.get("enum")
    if isinstance(enum_val, list) and None not in enum_val:
        out["enum"] = [*enum_val, None]
    return out


@deal.ensure(lambda params, result: (not isinstance(params, dict) or not params) or isinstance(result, dict))
@deal.ensure(lambda params, result: not isinstance(result, dict) or result.get("required") != [])
def _normalize_schema_for_strict_providers(params: Any) -> Any:
    """Normalize JSON Schema for strict upstream validators (Gemini, Groq, etc.).

    - Optional scalar properties, including a union of scalars, get ``null``
      so models may pass ``null``. Every other source type stays.
    - Empty ``required`` is removed so providers do not complain about required[0/1] missing.
    - ``properties`` is walked whenever it is a dict, even with no ``type``.
      ``items`` is kept when ``type`` is missing or includes ``array``.
    """
    # crosshair: off
    if type(params) is not dict or len(params) == 0:
        return params
    # Bugfix: #2594 removed this copy, so normalization mutated the caller's
    # schema in place (e.g. ToolBase.parameters class dicts shared by every
    # call). Copy first so the input is never changed.
    params = copy.deepcopy(params)
    # Dedupe only. One remaining name becomes a string. An empty list
    # becomes "string". Do not collapse a real union to one member:
    # ["string", "number"] must stay both, and ["array", "string"] must
    # not become "array" (apply_document_content accepts either).
    if "type" in params and isinstance(params["type"], list):
        types = params["type"]
        if not types:
            params["type"] = "string"
        else:
            seen = list(dict.fromkeys(types))
            if len(seen) == 1:
                params["type"] = seen[0]
            elif len(seen) != len(types):
                params["type"] = seen
    type_value = params.get("type")
    # Drop ``items`` only when ``type`` is present and does not include
    # ``array``. ``type != "array"`` is also true when ``type`` is missing
    # and when a union still lists ``array`` (``["array", "string"]`` or
    # ``["array", "null"]``). Popping ``items`` then hides the element
    # schema. No ``type`` plus ``items`` is an implicit array, and a union
    # ``validate`` accepts must keep its element schema.
    if "items" in params and type_value is not None and not _schema_type_includes_array(type_value):
        params.pop("items", None)
    if params.get("required") == []:
        params.pop("required", None)

    # Bugfix: When 'required' was a string (e.g. "ab"), set(params.get("required") or [])
    # split it into individual characters {'a', 'b'}, incorrectly treating properties as required.
    # Treat required as keys only when it is a list; leave non-list required intact in params.
    required_raw = params.get("required")
    required_keys = set(required_raw) if isinstance(required_raw, list) else set()

    # Walk whenever ``properties`` is a dict, then walk ``items`` too.
    # JSON Schema still describes an object when ``type`` is omitted.
    # Requiring ``type == "object"`` returns ``{"properties": {...}}``
    # untouched, so children never get nullable or union normalization.
    if isinstance(params.get("properties"), dict):
        new_props = {}
        for k, v in params["properties"].items():
            v = _normalize_schema_for_strict_providers(v)
            if k not in required_keys:
                v = _make_optional_scalar_nullable(v)
            new_props[k] = v
        params["properties"] = new_props
    if "items" in params:
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
                fov["type"] = ["string", "array"]
                fov["items"] = {"type": ["string", "number"]}
                desc_bits = fov.get("description") or ""
                if "Native JSON array" not in desc_bits:
                    fov["description"] = ((desc_bits + " " if desc_bits else "") + "Native JSON array of strings/numbers is accepted (same length as the range); a single string still fills the entire range.").strip()
        # Execute already coerces a bare range string to [str]. Source schemas stay
        # array-only; widen MCP so a host may send the string execute already wraps.
        rn = props.get("range")
        if isinstance(rn, dict) and rn.get("type") == "array":
            rn["type"] = ["string", "array"]
    return {"name": tool.name, "description": desc, "inputSchema": input_schema}
