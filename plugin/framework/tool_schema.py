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
converts each top-level argument to its declared schema type and folds enum
spellings; rules are listed on the function. ``call_properties`` /
``without_unknown_kwargs`` are the one allow-list for a call: an empty
``properties`` object is a no-arg schema. ``normalize_outbound_tool_calls``
applies that allow-list once, before any provider shim builds a request.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Sequence
from typing import Any, TypeVar

from plugin.framework.deal_shim import deal


_SCALAR_TYPES = frozenset({"integer", "number", "boolean", "string"})
_OutboundMessages = TypeVar("_OutboundMessages", bound=Sequence[Any])
_INT_RE = re.compile(r"[+-]?[0-9]+", re.ASCII)
_NUMBER_RE = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?", re.ASCII)
_TRUE_WORDS = frozenset({"true", "yes", "1"})
_FALSE_WORDS = frozenset({"false", "no", "0"})


def value_matches_type(value: Any, schema_type: Any) -> bool:
    """True when *value* fits a JSON Schema ``type`` (string or union list).

    A missing type is accepted. An unknown name such as ``"str"`` matches
    nothing. Unions accept any listed JSON Schema member.
    """
    if schema_type is None:
        return True
    types = schema_type if isinstance(schema_type, list) else [schema_type]
    for one in types:
        if one == "string" and isinstance(value, str):
            return True
        if one == "integer" and type(value) is int:
            return True
        if one == "number" and type(value) in (int, float):
            return True
        if one == "boolean" and type(value) is bool:
            return True
        if one == "array" and isinstance(value, list):
            return True
        if one == "object" and isinstance(value, dict):
            return True
        if one == "null" and value is None:
            return True
    return False


def _type_includes(type_value: Any, name: str) -> bool:
    """True when a JSON-schema ``type`` is *name* or a list that includes it."""
    if type_value == name:
        return True
    return isinstance(type_value, list) and name in type_value


def call_properties(schema: Any) -> dict[str, Any] | None:
    """Properties object for checking a call, including ``{}``.

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


def required_keys(schema: Any) -> list[Any]:
    """``required`` when it is a list, else [].

    Only a list is a set of keys; non-list values return [].
    """
    raw = schema.get("required") if isinstance(schema, dict) else None
    return raw if isinstance(raw, list) else []


def without_unknown_kwargs(schema: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the schema does not declare.

    Returns *kwargs* unchanged when every key is allowed, so callers can
    keep the original argument text when nothing was hallucinated.
    """
    props = call_properties(schema)
    if props is None:
        return kwargs
    if all(key in props for key in kwargs):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in props}


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


def normalize_outbound_tool_calls(messages: _OutboundMessages, tools: Any) -> _OutboundMessages:
    """Schema-check tool-call arguments before a provider shim sees them.

    One allow-list for every provider. ``properties: {}`` drops hallucinated
    kwargs on a no-arg tool. Duplicate tool names union their properties
    instead of keeping only the last schema.

    *messages* and *tools* may be any sequence (strings are not sequences
    of messages or tools).

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
    for message in messages:
        if not isinstance(message, dict):
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not isinstance(name, str) or name not in schemas:
                continue
            old = fn.get("arguments")
            new_args = _strip_call_arguments(schemas[name], old)
            # A missing key must stay missing; only assign when arguments changed.
            if new_args is not old:
                fn["arguments"] = new_args
    return messages


def _from_string(schema_type: Any, s: str) -> Any:
    """Convert a string argument to a non-string JSON Schema type when unambiguous."""
    if _type_includes(schema_type, "integer") or _type_includes(schema_type, "number"):
        stripped = s.strip()
        parsed: int | float | None = None
        if _INT_RE.fullmatch(stripped):
            try:
                parsed = int(stripped)
            except ValueError:
                parsed = None
        elif _NUMBER_RE.fullmatch(stripped):
            f = float(stripped)
            if math.isfinite(f):
                parsed = f
        if parsed is not None:
            if _type_includes(schema_type, "number"):
                if _type_includes(schema_type, "integer") and isinstance(parsed, float) and parsed.is_integer():
                    return int(parsed)
                return parsed
            if isinstance(parsed, int):
                return parsed
            if isinstance(parsed, float) and parsed.is_integer():
                return int(parsed)

    if _type_includes(schema_type, "boolean"):
        lowered = s.strip().casefold()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False

    if _type_includes(schema_type, "array") or _type_includes(schema_type, "object"):
        stripped = s.strip()
        if stripped.startswith(("[", "{")):
            try:
                parsed_json = json.loads(stripped)
            except json.JSONDecodeError:
                parsed_json = None
            if isinstance(parsed_json, list) and _type_includes(schema_type, "array"):
                return parsed_json
            if isinstance(parsed_json, dict):
                if _type_includes(schema_type, "object"):
                    return parsed_json
                if _type_includes(schema_type, "array"):
                    return [parsed_json]
        if _type_includes(schema_type, "array"):
            return [s]

    return s


def _to_declared_type(schema_type: Any, value: Any) -> Any:
    """Convert *value* toward *schema_type* when it does not already match."""
    if isinstance(value, str):
        return _from_string(schema_type, value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and value.is_integer() and _type_includes(schema_type, "integer"):
            return int(value)
        if type(value) is int and value in (0, 1) and _type_includes(schema_type, "boolean"):
            return bool(value)
        if _type_includes(schema_type, "string"):
            return str(value)
    if value is not None and not isinstance(value, list) and _type_includes(schema_type, "array"):
        return [value]
    return value


def _fold(text: str) -> str:
    """Normalize enum text: strip, casefold, and collapse spaces/hyphens to '_'."""
    return re.sub(r"[\s-]+", "_", text.strip().casefold())


def _one_edit_apart(a: str, b: str) -> bool:
    """True when *a* and *b* differ by one insert, delete, substitute, or adjacent swap."""
    la, lb = len(a), len(b)
    if la == lb:
        for i in range(la):
            if a[i] != b[i]:
                if a[i + 1 :] == b[i + 1 :]:
                    return True
                return i + 1 < la and a[i] == b[i + 1] and a[i + 1] == b[i] and a[i + 2 :] == b[i + 2 :]
        return False
    if la + 1 == lb:
        for i in range(la):
            if a[i] != b[i]:
                return a[i:] == b[i + 1 :]
        return True
    if la == lb + 1:
        return _one_edit_apart(b, a)
    return False


def _match_enum(value: str, enum: list[Any]) -> str:
    """Return the unique enum member matching *value* under fold, plural, or one-edit rules."""
    members = [m for m in enum if isinstance(m, str)]
    if not members:
        return value
    fv = _fold(value)
    folded = [(m, _fold(m)) for m in members]

    exact = [m for m, fm in folded if fm == fv]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return value

    plural = [m for m, fm in folded if fv + "s" == fm or fm + "s" == fv]
    if len(plural) == 1:
        return plural[0]
    if len(plural) > 1:
        return value

    if len(fv) >= 5:
        near = [m for m, fm in folded if _one_edit_apart(fv, fm)]
        if len(near) == 1:
            return near[0]

    return value


def _items_to_declared_type(prop: dict[str, Any], values: list[Any]) -> list[Any]:
    """Convert elements toward a single scalar ``items.type``; union and nested item types stay for the tool."""
    items = prop.get("items")
    item_type = items.get("type") if isinstance(items, dict) else None
    if not isinstance(item_type, str) or item_type not in _SCALAR_TYPES:
        return values
    return [v if value_matches_type(v, item_type) else _to_declared_type(item_type, v) for v in values]


def coerce_call_args(props: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Coerce top-level tool arguments toward their declared JSON Schema types and enums.

    Returns a new dict without mutating *kwargs*. Elements of any list (sent by
    the caller or produced by coercion) convert when ``items.type`` is one
    scalar type; union, object, and array items, and nested ``properties``, are
    not walked. Rules applied per property in order:
    - Skip type coercion when ``type`` is missing or ``value_matches_type`` already holds; unions that include ``string`` keep strings so the tool owns string parsing (such as forms ``items``).
    - Numeric strings match ASCII digits and finite floats only (Python ``int()``/``float()`` accept ``1_0``, full-width digits, ``nan``, and ``inf``); ``integer`` accepts whole values only.
    - Boolean words (``true``/``yes``/``1`` and ``false``/``no``/``0``, case-insensitive) convert to ``bool``.
    - JSON strings starting with ``[`` or ``{`` parse for ``array`` or ``object``; dicts wrap into ``[dict]`` for array-only.
    - Any other non-list string (or non-None scalar/dict) becomes ``[value]`` when ``type`` includes ``array`` and is never split on commas (HTML and formulas contain commas).
    - Whole floats convert to ``int`` for ``integer``; ints ``0``/``1`` convert to ``bool`` for ``boolean``.
    - Non-bool numbers (``int``/``float``) convert to ``str(value)`` when ``type`` includes ``string``.
    - String enum misses map only on a unique match (never pick a member silently) by fold, plural ``s``, or one-edit match with a 5-character floor (short members such as ``line``/``pie`` and ``odt``/``pdf`` are within two edits of each other).
    """
    if not isinstance(kwargs, dict):
        return kwargs
    if not isinstance(props, dict):
        return dict(kwargs)
    out = dict(kwargs)
    for k, v in out.items():
        prop = props.get(k)
        if not isinstance(prop, dict):
            continue
        schema_type = prop.get("type")
        if schema_type is not None and not value_matches_type(v, schema_type):
            v = _to_declared_type(schema_type, v)
        if isinstance(v, list):
            v = _items_to_declared_type(prop, v)
        enum = prop.get("enum")
        if isinstance(v, str) and isinstance(enum, list) and enum and v not in enum:
            v = _match_enum(v, enum)
        out[k] = v
    return out


@deal.ensure(lambda prop_schema, result, for_mcp=False: not isinstance(prop_schema, dict) or isinstance(result, dict))
def _make_optional_scalar_nullable(prop_schema: dict[str, Any], *, for_mcp: bool = False) -> dict[str, Any]:
    """Add null to optional scalar property types (strict providers reject bare null otherwise).

    A union of scalars is optional in the same way as one scalar. Null is
    appended; the other members stay. ``["string", "number"]`` must not
    become ``["string", "null"]``.

    MCP/Gemini rejects JSON null inside ``enum`` when agy forwards tool schemas.
    Optional enum properties stay non-nullable on the MCP wire; callers omit the key.
    """
    # crosshair: off
    if not isinstance(prop_schema, dict):
        return prop_schema
    if for_mcp and isinstance(prop_schema.get("enum"), list):
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


@deal.ensure(lambda params, result, for_mcp=False: (not isinstance(params, dict) or not params) or isinstance(result, dict))
@deal.ensure(lambda params, result, for_mcp=False: not isinstance(result, dict) or result.get("required") != [])
def _normalize_schema_for_strict_providers(params: Any, *, for_mcp: bool = False) -> Any:
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
    # Deepcopy parameters so normalization does not mutate the caller's schema.
    params = copy.deepcopy(params)
    # Dedupe only. One remaining name becomes a string. An empty list
    # becomes "string". Do not collapse a real union to one member:
    # ["string", "number"] must stay both, and ["array", "string"] must
    # not become "array" (apply_content accepts either).
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
    if "items" in params and type_value is not None and not _type_includes(type_value, "array"):
        params.pop("items", None)
    if params.get("required") == []:
        params.pop("required", None)

    # Walk whenever ``properties`` is a dict, then walk ``items`` too.
    # JSON Schema still describes an object when ``type`` is omitted.
    # Requiring ``type == "object"`` returns ``{"properties": {...}}``
    # untouched, so children never get nullable or union normalization.
    if isinstance(params.get("properties"), dict):
        req = required_keys(params)
        for k, v in params["properties"].items():
            v = _normalize_schema_for_strict_providers(v, for_mcp=for_mcp)
            if k not in req:
                v = _make_optional_scalar_nullable(v, for_mcp=for_mcp)
            params["properties"][k] = v
    if "items" in params and isinstance(params["items"], dict):
        params["items"] = _normalize_schema_for_strict_providers(params["items"], for_mcp=for_mcp)
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
                "name": "get_tree",
                "description": "...",
                "parameters": { ... JSON Schema ... }
            }
        }
    """
    # Host Tool + deepcopy of nested JSON Schema; pytest still checks @deal.
    # crosshair: off
    params = copy.copy(tool.get_parameters(doc_type) or {})
    params.setdefault("type", "object")
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
    input_schema.setdefault("type", "object")
    props = input_schema.setdefault("properties", {})
    props.setdefault("document_url", {
        "type": "string",
        "description": "Optional URL or RuntimeUID of the target document (both come from document_list_open). If not provided, the active document is used. A RuntimeUID also targets unsaved/untitled documents that have no file URL yet.",
    })
    desc = tool.get_description(doc_type)

    agent_label_for = getattr(tool, "agent_label_for", None)
    agent_label = agent_label_for(doc_type=doc_type) if callable(agent_label_for) else getattr(tool, "_agent_label", None)
    if agent_label and (callable(agent_label_for) or getattr(tool, "_special_base_class", None) is not None):
        from plugin.framework.prompts import format_specialized_domains_description

        # For MCP schemas, use a compact description to avoid duplicating the long domain list
        # (the detailed domain guidance lives in the 'domain' property description instead).
        # The full verbose guidance with examples is still used in chat system prompts.
        desc = f"{desc} See the 'domain' property for available areas and the 'task' parameter rules.".strip()

        if "domain" in props and isinstance(props["domain"], dict):
            props["domain"]["description"] = format_specialized_domains_description(agent_label=agent_label)

    input_schema = _normalize_schema_for_strict_providers(input_schema, for_mcp=True)
    props = input_schema["properties"]
    # coerce_call_args wraps a bare string for any array property, so widen
    # MCP range to string|array to match what execute already accepts.
    rn = props.get("range")
    if isinstance(rn, dict) and rn.get("type") == "array":
        rn["type"] = ["string", "array"]
    return {"name": tool.name, "description": desc, "inputSchema": input_schema}
