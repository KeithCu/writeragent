# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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
#
# Portions ported from Hermes Agent:
# MIT License
# Copyright (c) 2025 Nous Research
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# collapse_const_unions ported via Hermes from block/goose tool_schema_normalize.rs,
# Apache License 2.0, Copyright 2024 Block, Inc.
"""Pure JSON-schema normalization, $ref inlining, and tool naming for remote MCP tools."""

from __future__ import annotations

import copy
import hashlib
import logging
import re
from typing import Any, Callable, NamedTuple
from urllib.parse import unquote

from plugin.mcp_client.client_helpers import strip_tool_metadata_text

log = logging.getLogger("writeragent.mcp_client")

MCP_TOOL_NAME_PREFIX = "mcp__"
_MCP_TOOL_NAME_MAX_LENGTH = 64
_MCP_TOOL_NAME_HASH_LENGTH = 8
MAX_INLINED_NODES = 5000

# Mapping-valued JSON Schema keywords: the value maps entry names to schemas and is never a schema node itself.
_SCHEMA_NAME_MAP_KEYS = frozenset({"properties", "patternProperties", "dependentSchemas"})
_DEF_CONTAINER_KEYS = frozenset({"$defs", "definitions"})
_SCHEMA_MAP_KEYS = _SCHEMA_NAME_MAP_KEYS | _DEF_CONTAINER_KEYS
# Strings the server matches calls against stay byte-exact.
_SCHEMA_CONTRACT_KEYS = frozenset({"enum", "const", "required", "$ref", "pattern", "dependentRequired", "discriminator"})
# Literal instance data, not schema: a keyword-named key inside it is plain data.
_SCHEMA_DATA_KEYS = frozenset({"default", "examples"})
_LITERAL_VALUE_KEYS = _SCHEMA_DATA_KEYS | frozenset({"enum", "const"})

# Anthropic (and Bedrock/Vertex/Azure fronting it) reject property keys not matching this; one bad
# key anywhere in the tools array 400s the request.
_PROP_KEY_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")
_PROP_KEY_BAD_CHARS = re.compile(r"[^a-zA-Z0-9_.-]")
_UNION_KEYS = ("anyOf", "oneOf")
_UNION_META_KEYS = ("title", "description", "default", "examples")

# Keywords JSON Schema applies only to values of one type; branches whose extra keys are all
# in their own type's set can be merged into one type-list node and never loosen what validates
# (integer <= number, so a number branch's numeric bounds may tighten an integer branch).
_TYPE_KEYWORDS: dict[str, frozenset[str]] = {
    "array": frozenset({"items", "minItems", "maxItems", "uniqueItems"}),
    "object": frozenset({"properties", "required", "additionalProperties", "minProperties", "maxProperties"}),
    "string": frozenset({"minLength", "maxLength", "pattern", "format"}),
    "number": frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}),
    "integer": frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}),
    "boolean": frozenset(),
}

# RFC 6901: an array index is "0" or a number without leading zeros; "~" escapes only "~0" and "~1".
_POINTER_INDEX = re.compile(r"0|[1-9][0-9]*")
_BAD_POINTER_ESCAPE = re.compile(r"~(?![01])")

_CONST_PRIMITIVE_TYPES: dict[type, str] = {
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
}

_BARE_TYPE_NAMES = frozenset({"object", "string", "number", "integer", "boolean", "array", "null"})
_NON_SCHEMA_LIST_KEYS = frozenset({"required", "enum", "examples", "dependentRequired"})
_SCHEMA_CHILD_KEYS = frozenset({
    "items",
    "additionalItems",
    "additionalProperties",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
    "anyOf",
    "oneOf",
    "allOf",
    "prefixItems",
})


class ToolCandidate(NamedTuple):
    """One remote tool candidate prior to cross-server name collision resolution."""

    registry_name: str
    origin: str
    server: dict[str, Any]
    spec: dict[str, Any]


def _empty_object() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "required": []}


def _rewrite(schema: Any, fn: Callable[[dict[str, Any]], Any]) -> Any:
    """Bottom-up map over a schema tree: lists and dicts recurse, then *fn* sees each dict."""
    if isinstance(schema, list):
        return [_rewrite(item, fn) for item in schema]
    if not isinstance(schema, dict):
        return schema
    return fn({k: _rewrite(v, fn) for k, v in schema.items()})


def sanitize_mcp_name_component(value: str) -> str:
    """Replace every char outside ``[A-Za-z0-9_]`` with ``_`` so generated names pass provider validation."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))


def mcp_prefixed_tool_name(server_name: str, tool_name: str) -> str:
    """Wire name ``mcp__<server>__<tool>``, clamped to 64 chars with a stable hash suffix when longer.

    OpenAI-compatible providers validate function names against ``^[a-zA-Z0-9_-]{1,64}$`` and 400
    the whole request when any generated name exceeds 64 characters.
    """
    full_name = f"{MCP_TOOL_NAME_PREFIX}{sanitize_mcp_name_component(server_name)}__{sanitize_mcp_name_component(tool_name)}"
    if len(full_name) <= _MCP_TOOL_NAME_MAX_LENGTH:
        return full_name
    suffix = "_" + hashlib.sha256(full_name.encode("utf-8")).hexdigest()[:_MCP_TOOL_NAME_HASH_LENGTH]
    log.debug(
        "MCP tool name %r (%d chars) exceeds the %d-char provider limit; shortened with a hash suffix",
        full_name,
        len(full_name),
        _MCP_TOOL_NAME_MAX_LENGTH,
    )
    return full_name[: _MCP_TOOL_NAME_MAX_LENGTH - len(suffix)] + suffix


def _is_null_branch(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "null"


def _carry_union_meta(outer: dict[str, Any], replacement: dict[str, Any], *, skip_default_on_ref: bool) -> None:
    """Copy outer-union metadata onto *replacement* where absent.

    Fireworks rejects ``default`` beside ``$ref``, hence ``skip_default_on_ref``.
    """
    for meta_key in _UNION_META_KEYS:
        if meta_key in outer and meta_key not in replacement and not (
            skip_default_on_ref and meta_key == "default" and "$ref" in replacement
        ):
            replacement[meta_key] = outer[meta_key]


def strip_nullable_unions(schema: Any, *, keep_nullable_hint: bool = True) -> Any:
    """Collapse ``anyOf``/``oneOf`` nullable unions to the single non-null branch.

    Anthropic rejects ``{"type": "null"}`` branches; optionality already lives in the parent's
    ``required``, and ``nullable: true`` is kept as a hint.
    """

    def collapse(stripped: dict[str, Any]) -> Any:
        for key in _UNION_KEYS:
            variants = stripped.get(key)
            if not isinstance(variants, list):
                continue
            non_null = [item for item in variants if not _is_null_branch(item)]
            if len(non_null) == 1 and len(non_null) != len(variants):
                replacement = dict(non_null[0]) if isinstance(non_null[0], dict) else {}
                if keep_nullable_hint:
                    replacement.setdefault("nullable", True)
                _carry_union_meta(stripped, replacement, skip_default_on_ref=True)
                return _rewrite(replacement, collapse)
        return stripped

    return _rewrite(schema, collapse)


def _const_branch_type(branch: Any) -> str | None:
    """Primitive JSON-Schema type of a pure ``const`` branch, else None."""
    if not isinstance(branch, dict) or "const" not in branch or set(branch) - {"const", "type", "title", "description"}:
        return None
    # ``type(value)`` lookup (not isinstance): bool is a subclass of int.
    json_type = _CONST_PRIMITIVE_TYPES.get(type(branch["const"]))
    return json_type if branch.get("type") in (None, json_type) else None


def collapse_const_unions(schema: Any) -> Any:
    """Collapse ``anyOf``/``oneOf`` unions of same-typed consts to ``enum`` without mutating input."""

    def collapse(out: dict[str, Any]) -> Any:
        for key in _UNION_KEYS:
            variants = out.get(key)
            if not isinstance(variants, list) or not variants:
                continue
            null_branches = [i for i in variants if _is_null_branch(i) and "const" not in i]
            const_branches = [item for item in variants if item not in null_branches]
            if len(null_branches) > 1 or not const_branches:
                continue
            branch_types = {_const_branch_type(item) for item in const_branches}
            if len(branch_types) != 1 or None in branch_types:
                continue
            replacement: dict[str, Any] = {
                "type": branch_types.pop(),
                "enum": [item["const"] for item in const_branches],
            }
            if null_branches:
                replacement["nullable"] = True
            _carry_union_meta(out, replacement, skip_default_on_ref=False)
            return replacement
        return out

    return _rewrite(schema, collapse)


def _repair_object_shape(node: Any, *, _object_root: bool = False) -> Any:
    """Fill missing object ``type``/``properties`` and prune dangling ``required`` entries.

    Gemini 400s when ``required`` names a property missing from ``properties``, and strict
    OpenAI-compatible proxies read a missing or non-list ``required`` as ``null`` instead of
    ``[]``. Constraint fragments (``required`` without ``properties`` or ``type``) are left
    alone outside the root so ``oneOf``/``anyOf`` presence rules are not collapsed into
    always-true schemas.
    """
    if isinstance(node, list):
        return [_repair_object_shape(item) for item in node]
    if not isinstance(node, dict):
        return node
    repaired: dict[str, Any] = {}
    for key, value in node.items():
        if key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            repaired[key] = {name: _repair_object_shape(schema) for name, schema in value.items()}
        else:
            repaired[key] = _repair_object_shape(value)
    if _object_root or "properties" in repaired or "type" in node:
        if not repaired.get("type") and ("properties" in repaired or "required" in repaired):
            repaired["type"] = "object"
        if repaired.get("type") == "object":
            if not isinstance(repaired.get("properties"), dict):
                repaired["properties"] = {}
            required = repaired.get("required")
            props = repaired.get("properties") or {}
            repaired["required"] = (
                [r for r in required if isinstance(r, str) and r in props] if isinstance(required, list) else []
            )
    return repaired


def normalize_mcp_input_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize an MCP ``inputSchema`` for strict providers while keeping ``$defs``/``definitions``.

    ``inline_local_refs`` resolves both ``#/$defs`` and ``#/definitions``, and neither key reaches
    the wire.
    """
    if not schema:
        return _empty_object()
    normalized = strip_nullable_unions(schema, keep_nullable_hint=True)
    normalized = collapse_const_unions(normalized)
    normalized = _repair_object_shape(normalized, _object_root=True)
    if not isinstance(normalized, dict):
        return _empty_object()
    if normalized.get("type") == "object" and "properties" not in normalized:
        normalized = {**normalized, "properties": {}}
    return normalized


def _strip_schema_child(
    key: str,
    value: Any,
    tool_name: str,
    contract: bool,
    data: bool,
    schema: bool,
) -> Any:
    if schema and key == "dependencies" and isinstance(value, dict):
        new = {name: strip_schema_annotations(dep, tool_name, isinstance(dep, list)) for name, dep in value.items()}
        return value if all(new[name] is dep for name, dep in value.items()) else new
    return strip_schema_annotations(
        value,
        tool_name,
        contract or (schema and key in _SCHEMA_CONTRACT_KEYS),
        schema and key in _SCHEMA_MAP_KEYS,
        data or (schema and key in _SCHEMA_DATA_KEYS),
    )


def strip_schema_annotations(
    node: Any,
    tool_name: str = "",
    _contract: bool = False,
    _names: bool = False,
    _data: bool = False,
) -> Any:
    """Strip invisible TAG/bidi chars from free-text schema strings while keeping contract values exact.

    Returns ``node`` itself when nothing changed.
    """
    if isinstance(node, str):
        if not _contract:
            return strip_tool_metadata_text(node)
        if strip_tool_metadata_text(node) != node:
            log.warning(
                "MCP tool '%s': schema name/enum value %r carries invisible characters; kept as-is",
                tool_name,
                node,
            )
        return node
    if isinstance(node, dict):
        if _names:
            for name in node:
                strip_schema_annotations(name, tool_name, True)
        schema = not (_names or _data or _contract)
        items = [(k, _strip_schema_child(k, v, tool_name, _contract, _data, schema)) for k, v in node.items()]
        return node if all(nv is v for (_, nv), v in zip(items, node.values())) else dict(items)
    if isinstance(node, list):
        items = [strip_schema_annotations(v, tool_name, _contract, False, _data) for v in node]
        return node if all(n is v for n, v in zip(items, node)) else items
    return node


def sanitize_property_key(key: str) -> str:
    """Deterministically map an arbitrary property key to one matching ``^[a-zA-Z0-9_.-]{1,64}$``."""
    return _PROP_KEY_BAD_CHARS.sub("_", key)[:64] or "param"


def _rename_property_keys(props: dict[str, Any], path: str) -> dict[str, str]:
    """Return ``{original_key: conforming_key}`` for one properties dict (identity entries omitted)."""
    renames: dict[str, str] = {}
    taken = {k for k in props if _PROP_KEY_RE.match(k)}
    for key in (k for k in props if not _PROP_KEY_RE.match(k)):
        base = sanitize_property_key(key)
        candidate, i = base, 2
        while candidate in taken:
            candidate, i = base[: 64 - len(f"_{i}")] + f"_{i}", i + 1
        taken.add(candidate)
        renames[key] = candidate
        log.debug("schema_sanitizer[%s]: renamed property key %r -> %r", path, key, candidate)
    return renames


def resolve_local_schema_ref(
    schema: Any,
    root_schema: Any,
    seen_refs: set[str] | None = None,
) -> Any:
    """Follow a document-local ``$ref`` in *schema* against *root_schema*.

    Remote, malformed, cyclic, or dangling refs come back unchanged.
    """
    if not isinstance(schema, dict) or not isinstance(root_schema, dict):
        return schema

    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#"):
        return schema
    pointer = unquote(ref[1:])
    if pointer and not pointer.startswith("/"):
        return schema
    seen = seen_refs or set()
    if ref in seen:
        return schema

    target: Any = root_schema
    for raw_token in pointer.split("/")[1:]:
        if _BAD_POINTER_ESCAPE.search(raw_token):
            return schema
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict) and token in target:
            target = target[token]
        elif isinstance(target, list) and _POINTER_INDEX.fullmatch(token) and int(token) < len(target):
            target = target[int(token)]
        else:
            return schema
    if not isinstance(target, dict):
        return schema

    resolved = resolve_local_schema_ref(target, root_schema, seen | {ref})
    siblings = {key: value for key, value in schema.items() if key != "$ref"}
    if not siblings or not isinstance(resolved, dict):
        return resolved
    combined = dict(resolved)
    for key, value in siblings.items():
        combined.setdefault(key, value)
    return combined


def unrename_tool_args(params_schema: Any, args: Any, root_schema: Any = None) -> Any:
    """Map sanitized keys in model-emitted *args* back to the server's original property names."""
    root = params_schema if root_schema is None else root_schema
    params_schema = resolve_local_schema_ref(params_schema, root)
    props = params_schema.get("properties") if isinstance(params_schema, dict) else None
    if not isinstance(props, dict) or not isinstance(args, dict):
        return args
    reverse = {v: k for k, v in _rename_property_keys(props, "<unrename>").items()}
    out: dict[str, Any] = {}
    for key, value in args.items():
        key_str = str(key)
        orig = reverse.get(key_str, key_str)
        sub = resolve_local_schema_ref(props[orig], root) if isinstance(props.get(orig), dict) else {}
        if isinstance(value, dict) and sub:
            value = unrename_tool_args(sub, value, root)
        elif isinstance(value, list) and isinstance(sub.get("items"), dict):
            value = [
                unrename_tool_args(sub["items"], item, root) if isinstance(item, dict) else item
                for item in value
            ]
        out[orig] = value
    return out


def inline_local_refs(schema: dict[str, Any], *, max_nodes: int = MAX_INLINED_NODES) -> dict[str, Any]:
    """Inline every local ``$ref`` and drop ``$defs``/``definitions`` containers.

    Raises ``ValueError`` on recursive, remote, or dangling ``$ref``s or when the expanded tree
    exceeds *max_nodes*.
    """
    budget = [max_nodes]

    def _inline(node: Any, seen: frozenset[str]) -> Any:
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError(f"MCP tool schema too large after $ref inlining (>{max_nodes} nodes)")
        if isinstance(node, list):
            return [_inline(item, seen) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or ref in seen:
                raise ValueError(f"recursive or invalid $ref: {ref!r}")
            resolved = resolve_local_schema_ref(node, schema)
            if not isinstance(resolved, dict) or "$ref" in resolved:
                raise ValueError(f"unresolvable $ref: {ref!r}")
            return _inline(resolved, seen | {ref})
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _DEF_CONTAINER_KEYS:
                continue
            if key in _SCHEMA_NAME_MAP_KEYS and isinstance(value, dict):
                out[key] = {prop_name: _inline(prop_schema, seen) for prop_name, prop_schema in value.items()}
            elif key in _LITERAL_VALUE_KEYS:
                out[key] = copy.deepcopy(value)
            else:
                out[key] = _inline(value, seen)
        return out

    result = _inline(schema, frozenset())
    return result if isinstance(result, dict) else _empty_object()


def _sanitize_node(node: Any, path: str) -> Any:
    """Recursively sanitize a JSON-Schema fragment for strict providers.

    llama.cpp's grammar converter fails on ``{"type": "object"}`` without ``properties`` and on
    bare-string schemas. WriterAgent sends ``type`` arrays as they are
    (``tool_schema._normalize_schema_for_strict_providers``), so ``type`` lists are copied
    literally.
    """
    if isinstance(node, str):
        if node in _BARE_TYPE_NAMES:
            log.debug("schema_sanitizer[%s]: replacing bare-string schema %r with {'type': %r}", path, node, node)
            return _empty_object() if node == "object" else {"type": node}
        log.debug("schema_sanitizer[%s]: replacing non-schema string %r with empty object schema", path, node)
        return _empty_object()
    if isinstance(node, list):
        return [_sanitize_node(item, f"{path}[{i}]") for i, item in enumerate(node)]
    if not isinstance(node, dict):
        return node
    props_in = node.get("properties")
    prop_renames = _rename_property_keys(props_in, f"{path}.properties") if isinstance(props_in, dict) else {}
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            renames = prop_renames if key == "properties" else {}
            out[key] = {
                renames.get(k, k): _sanitize_node(v, f"{path}.{key}.{renames.get(k, k)}")
                for k, v in value.items()
            }
        elif key == "dependencies" and isinstance(value, dict):
            out[key] = {
                k: _sanitize_node(v, f"{path}.{key}.{k}") if isinstance(v, dict) else copy.deepcopy(v)
                for k, v in value.items()
            }
        elif key in {"items", "additionalProperties"}:
            out[key] = value if isinstance(value, bool) else _sanitize_node(value, f"{path}.{key}")
        elif key in _NON_SCHEMA_LIST_KEYS:
            if key == "required" and isinstance(value, bool):
                continue
            if key == "required" and prop_renames and isinstance(value, list):
                out[key] = [prop_renames.get(r, r) if isinstance(r, str) else r for r in value]
            else:
                out[key] = copy.deepcopy(value) if isinstance(value, (list, dict)) else value
        elif key in _SCHEMA_CHILD_KEYS:
            out[key] = _sanitize_node(value, f"{path}.{key}")
        else:
            out[key] = copy.deepcopy(value)
    if isinstance(props_in, dict):
        lifted = [
            prop_renames.get(k, k)
            for k, v in props_in.items()
            if isinstance(v, dict) and v.get("required") is True
        ]
        if lifted:
            required = out.get("required", [])
            required = required if isinstance(required, list) else []
            out["required"] = required + [key for key in lifted if key not in required]
    if out.get("type") == "object":
        if not isinstance(out.get("properties"), dict):
            out["properties"] = {}
        required = out.get("required")
        out["required"] = (
            [r for r in required if isinstance(r, str) and r in out["properties"]] if isinstance(required, list) else []
        )
    return out


def resolve_name_collisions(candidates: list[ToolCandidate]) -> list[ToolCandidate]:
    """Deduplicate exact matches and fail closed on any multi-origin name collision."""
    unique: list[ToolCandidate] = []
    origins_by_name: dict[str, set[str]] = {}
    for candidate in candidates:
        origins = origins_by_name.setdefault(candidate.registry_name, set())
        if candidate.origin in origins:
            log.debug(
                "MCP duplicate registration candidate %s for '%s'; keeping one",
                candidate.origin,
                candidate.registry_name,
            )
            continue
        origins.add(candidate.origin)
        unique.append(candidate)
    ambiguous = {
        registry_name: sorted(origins)
        for registry_name, origins in origins_by_name.items()
        if len(origins) > 1
    }
    for registry_name, colliding_origins in sorted(ambiguous.items()):
        log.error(
            "MCP tool name collision for '%s' from %s; skipping every colliding entry",
            registry_name,
            ", ".join(colliding_origins),
        )
    return [c for c in unique if c.registry_name not in ambiguous]


def _check_arrays_have_items(node: Any, path: str) -> None:
    if isinstance(node, list):
        for i, item in enumerate(node):
            _check_arrays_have_items(item, f"{path}[{i}]")
        return
    if not isinstance(node, dict):
        return
    node_type = node.get("type")
    has_array = node_type == "array" or (isinstance(node_type, list) and "array" in node_type)
    if has_array and not isinstance(node.get("items"), dict):
        raise ValueError(f"MCP tool schema array at {path} is missing an 'items' schema")
    for key, value in node.items():
        if key in _LITERAL_VALUE_KEYS:
            continue
        if key in _SCHEMA_NAME_MAP_KEYS and isinstance(value, dict):
            for prop_name, prop_schema in value.items():
                _check_arrays_have_items(prop_schema, f"{path}.{key}.{prop_name}")
        else:
            _check_arrays_have_items(value, f"{path}.{key}")


def _merge_typed_branches(branches: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Merge differently-typed union branches into one type-list node when lossless."""
    if not branches:
        return None
    types: list[str] = []
    seen_types: set[str] = set()
    merged_keywords: dict[str, Any] = {}
    for branch in branches:
        branch_type = branch.get("type")
        if not isinstance(branch_type, str) or branch_type not in _TYPE_KEYWORDS or branch_type in seen_types:
            return None
        seen_types.add(branch_type)
        types.append(branch_type)
        allowed = _TYPE_KEYWORDS[branch_type]
        for key, value in branch.items():
            if key == "type" or key == "nullable" or key in _UNION_META_KEYS:
                continue
            if key not in allowed:
                return None
            if key in merged_keywords and merged_keywords[key] != value:
                return None
            merged_keywords[key] = value
    return {**merged_keywords, "type": types[0] if len(types) == 1 else types}


def _is_sendable(schema: dict[str, Any]) -> bool:
    """Return True if *schema* can survive ``to_smol_inputs`` -> ``get_tool_json_schema`` without provider 400s."""
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        types: list[Any] = [raw_type]
    elif isinstance(raw_type, list):
        types = raw_type
    else:
        return False
    non_null = [t for t in types if t != "null"]
    if not non_null or not all(isinstance(t, str) and t in _TYPE_KEYWORDS for t in non_null):
        return False
    if any(k in schema for k in ("anyOf", "oneOf", "allOf")):
        return False
    try:
        _check_arrays_have_items(schema, "")
    except ValueError:
        return False
    return True


def _collapse_parameter_union(prop: dict[str, Any]) -> dict[str, Any]:
    """Collapse a top-level ``anyOf``/``oneOf`` parameter into a sendable type-list or single branch.

    The vendored ``get_tool_json_schema`` in ``plugin/contrib/smolagents/models.py`` flattens
    top-level ``anyOf`` to ``type: [...]`` and drops every branch's ``items`` and ``properties``,
    which causes Gemini 400s. WriterAgent's convention for multi-type parameters is a ``type``
    list with ``items`` on the same node (see ``apply_document_content.py`` ``content``), matching
    Hermes's principle that every branch survives (``tools/schema_sanitizer.py:_normalize_type_array``)
    and Gemini's rule that ``items`` sits on the node declaring ``array``
    (``agent/gemini_schema.py:_normalize_gemini_type_array``).

    Nested unions pass through ``get_tool_json_schema`` untouched and are left alone so
    nested discriminated unions (e.g. ``object | object``) keep all branches. A single branch is
    used only when merging would loosen the schema (e.g. ``enum`` or same-typed branches).
    """
    for key in _UNION_KEYS:
        variants = prop.get(key)
        if not isinstance(variants, list):
            continue
        rest = {k: v for k, v in prop.items() if k != key}
        outer_meta = {k: v for k, v in rest.items() if k in _UNION_META_KEYS}
        non_meta_rest = {k: v for k, v in rest.items() if k not in _UNION_META_KEYS}
        branches = [item for item in variants if isinstance(item, dict) and not _is_null_branch(item)]
        merged = _merge_typed_branches(branches)
        candidates = ([merged] if merged is not None else []) + branches
        # Outer metadata wins; outer constraints must not loosen or override a branch's own constraints.
        collapsed = next(
            (
                candidate
                for c in candidates
                if not any(k in c and c[k] != v for k, v in non_meta_rest.items())
                and _is_sendable(candidate := {**non_meta_rest, **c, **outer_meta})
            ),
            None,
        )
        if collapsed is None:
            return prop
        prop = collapsed
    return prop


def _validate_wire_schema(parameters: dict[str, Any], tool_name: str) -> None:
    """Reject parameter shapes that ``to_smol_inputs`` / ``get_tool_json_schema`` would mangle or providers 400.

    Top-level ``anyOf``/``oneOf`` unions are collapsed by ``_collapse_parameter_union`` first; only
    unsendable unions or ``allOf`` remain here to be rejected. Typeless parameters are rejected
    because ``to_smol_inputs`` defaults a missing ``type`` to ``"any"``, which
    ``get_tool_json_schema`` maps to ``"string"``.
    """
    _check_arrays_have_items(parameters, tool_name or "<tool>")
    props = parameters.get("properties")
    if not isinstance(props, dict):
        return
    for prop_name, prop_schema in props.items():
        if not isinstance(prop_schema, dict):
            raise ValueError(f"MCP tool '{tool_name}': parameter {prop_name!r} has non-object schema")
        if any(k in prop_schema for k in ("anyOf", "oneOf", "allOf")):
            raise ValueError(
                f"MCP tool '{tool_name}': parameter {prop_name!r} has a union with no branch the model can be sent"
            )
        if "type" not in prop_schema:
            raise ValueError(f"MCP tool '{tool_name}': parameter {prop_name!r} has no 'type'")


def convert_mcp_schema(server_name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Convert one remote MCP tool spec into a native tool schema plus its ``dispatch_schema``."""
    tool_name = str(spec.get("name") or "")
    raw_input = spec.get("inputSchema")
    dispatch_schema = strip_schema_annotations(
        normalize_mcp_input_schema(raw_input if isinstance(raw_input, dict) else None),
        tool_name,
    )
    parameters = _sanitize_node(inline_local_refs(dispatch_schema), tool_name)
    props = parameters.get("properties")
    if isinstance(props, dict):
        parameters["properties"] = {
            name: _collapse_parameter_union(schema) if isinstance(schema, dict) else schema
            for name, schema in props.items()
        }
    _validate_wire_schema(parameters, tool_name)
    raw_desc = str(spec.get("description") or "").strip()
    description = strip_tool_metadata_text(raw_desc or f"MCP tool {tool_name} from {server_name}")
    return {
        "name": mcp_prefixed_tool_name(server_name, tool_name),
        "description": description,
        "parameters": parameters,
        "dispatch_schema": dispatch_schema,
    }
