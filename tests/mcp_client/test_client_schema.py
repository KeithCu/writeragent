# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.mcp_client.client_schema."""

from __future__ import annotations

import copy
import re

import pytest

from plugin.mcp_client.client_schema import (
    ToolCandidate,
    _sanitize_node,
    collapse_const_unions,
    convert_mcp_schema,
    inline_local_refs,
    mcp_prefixed_tool_name,
    normalize_mcp_input_schema,
    resolve_name_collisions,
    sanitize_property_key,
    strip_nullable_unions,
    strip_schema_annotations,
    unrename_tool_args,
)


def test_object_without_properties():
    out = _sanitize_node({"type": "object"}, "t")
    assert out == {"type": "object", "properties": {}, "required": []}


def test_nested_object_without_properties():
    schema = {
        "type": "object",
        "properties": {"meta": {"type": "object"}},
        "required": ["meta"],
    }
    out = _sanitize_node(schema, "t")
    assert out["properties"]["meta"] == {"type": "object", "properties": {}, "required": []}


def test_bare_string_object_value():
    schema = {
        "type": "object",
        "properties": {"raw": "object", "flag": "boolean", "mystery": "custom"},
    }
    out = _sanitize_node(schema, "t")
    assert out["properties"]["raw"] == {"type": "object", "properties": {}, "required": []}
    assert out["properties"]["flag"] == {"type": "boolean"}
    assert out["properties"]["mystery"] == {"type": "object", "properties": {}, "required": []}


def test_anyof_nested_objects():
    schema = {
        "type": "object",
        "properties": {"payload": {"anyOf": [{"type": "object"}, {"type": "string"}]}},
    }
    out = _sanitize_node(schema, "t")
    assert out["properties"]["payload"]["anyOf"][0] == {"type": "object", "properties": {}, "required": []}
    assert out["properties"]["payload"]["anyOf"][1] == {"type": "string"}


def test_required_pruned():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a", "missing", 123],
    }
    out = _sanitize_node(schema, "t")
    assert out["required"] == ["a"]


def test_empty_required_survives():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    out = _sanitize_node(schema, "t")
    assert out["required"] == []


def test_well_formed_unchanged():
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File path"}},
        "required": ["path"],
    }
    assert _sanitize_node(schema, "t") == schema


def test_additional_properties_and_items_sanitized():
    schema = {
        "type": "object",
        "properties": {
            "arr": {"type": "array", "items": {"type": "object"}},
            "extra": {"type": "object", "additionalProperties": {"type": "object"}},
            "flag": {"type": "object", "additionalProperties": False},
        },
    }
    out = _sanitize_node(schema, "t")
    assert out["properties"]["arr"]["items"] == {"type": "object", "properties": {}, "required": []}
    assert out["properties"]["extra"]["additionalProperties"] == {"type": "object", "properties": {}, "required": []}
    assert out["properties"]["flag"]["additionalProperties"] is False


def test_sanitize_property_key_empty_falls_back():
    assert sanitize_property_key("") == "param"
    assert sanitize_property_key("file path!") == "file_path_"
    assert len(sanitize_property_key("x" * 100)) == 64


def test_dependent_required_preserved_and_not_mutated():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "dependentRequired": {"a": ["b"]},
    }
    snap = copy.deepcopy(schema)
    out = _sanitize_node(schema, "t")
    assert out["dependentRequired"] == {"a": ["b"]}
    assert schema == snap


def test_dependent_schemas_recursed():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "dependentSchemas": {"a": {"type": "object"}},
    }
    out = _sanitize_node(schema, "t")
    assert out["dependentSchemas"]["a"] == {"type": "object", "properties": {}, "required": []}


def test_collapse_const_unions_cases():
    # Pure const union (anyOf and oneOf)
    assert collapse_const_unions({"anyOf": [{"const": "red"}, {"const": "green"}]}) == {
        "type": "string",
        "enum": ["red", "green"],
    }
    assert collapse_const_unions({"oneOf": [{"const": 1}, {"const": 2}]}) == {
        "type": "integer",
        "enum": [1, 2],
    }

    # Mixed (non-const branch) left alone
    mixed = {"anyOf": [{"const": "red"}, {"type": "string"}]}
    assert collapse_const_unions(mixed) == mixed

    # Non-uniform types left alone
    non_uniform = {"anyOf": [{"const": "red"}, {"const": 1}]}
    assert collapse_const_unions(non_uniform) == non_uniform

    # Bool vs int never merges
    bool_int = {"anyOf": [{"const": True}, {"const": 1}]}
    assert collapse_const_unions(bool_int) == bool_int

    # Nested
    nested = {
        "type": "object",
        "properties": {"c": {"anyOf": [{"const": "a"}, {"const": "b"}]}},
    }
    assert collapse_const_unions(nested)["properties"]["c"] == {"type": "string", "enum": ["a", "b"]}

    # Outer metadata carried
    outer_meta = {
        "title": "Color",
        "description": "Pick one",
        "default": "red",
        "anyOf": [{"const": "red"}, {"const": "blue"}],
    }
    assert collapse_const_unions(outer_meta) == {
        "type": "string",
        "enum": ["red", "blue"],
        "title": "Color",
        "description": "Pick one",
        "default": "red",
    }

    # Branch metadata (title/description) tolerated; extra branch keys disqualify
    branch_meta = {"anyOf": [{"const": "x", "title": "X"}, {"const": "y", "description": "Y"}]}
    assert collapse_const_unions(branch_meta) == {"type": "string", "enum": ["x", "y"]}
    extra_key = {"anyOf": [{"const": "x", "minLength": 1}, {"const": "y"}]}
    assert collapse_const_unions(extra_key) == extra_key

    # Mismatched declared type disqualifies
    mismatch = {"anyOf": [{"const": "x", "type": "integer"}, {"const": "y"}]}
    assert collapse_const_unions(mismatch) == mismatch

    # Null + const with nullable strip
    null_const = {"anyOf": [{"const": "a"}, {"const": "b"}, {"type": "null"}]}
    collapsed = collapse_const_unions(strip_nullable_unions(null_const))
    assert collapsed == {"type": "string", "enum": ["a", "b"], "nullable": True}


def test_normalize_collapses_const_unions_without_mutating_input():
    raw = {
        "type": "object",
        "properties": {
            "mode": {"anyOf": [{"const": "fast"}, {"const": "safe"}]},
        },
        "required": ["mode"],
    }
    snap = copy.deepcopy(raw)
    out = normalize_mcp_input_schema(raw)
    assert out["properties"]["mode"] == {"type": "string", "enum": ["fast", "safe"]}
    assert raw == snap


def test_normalize_preserves_constraint_fragments_and_repairs_declared_objects():
    raw = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "object"}},
        "oneOf": [{"required": ["a"]}, {"required": ["b"]}],
    }
    out = normalize_mcp_input_schema(raw)
    assert out["oneOf"] == [{"required": ["a"]}, {"required": ["b"]}]
    assert out["properties"]["b"] == {"type": "object", "properties": {}, "required": []}


def test_definitions_as_property_name_preserved():
    schema = {
        "type": "object",
        "properties": {
            "definitions": {"type": "string"},
            "$defs": {"type": "integer"},
        },
        "$defs": {"Unused": {"type": "string"}},
        "definitions": {"AlsoUnused": {"type": "string"}},
    }
    inlined = inline_local_refs(schema)
    assert "definitions" in inlined["properties"]
    assert "$defs" in inlined["properties"]
    assert "$defs" not in inlined
    assert "definitions" not in inlined


def test_normalized_schema_keeps_required_as_list():
    out = normalize_mcp_input_schema({"type": "object", "properties": {"a": {"type": "string"}}})
    assert out["required"] == []


def test_optional_nullable_collapsed():
    out = normalize_mcp_input_schema(
        {
            "type": "object",
            "properties": {
                "opt": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
            },
        }
    )
    assert out["properties"]["opt"] == {"type": "string", "nullable": True, "default": None}


def test_long_names_clamped_to_64():
    name = mcp_prefixed_tool_name("very_long_server_name_exceeding_limits", "very_long_tool_name_exceeding_limits")
    assert len(name) == 64
    assert name == mcp_prefixed_tool_name("very_long_server_name_exceeding_limits", "very_long_tool_name_exceeding_limits")


def test_same_name_normalization_collision_skips_every_colliding_entry():
    c1 = ToolCandidate("mcp__my_srv__run", "server 'my-srv' tool 'run'", {"name": "my-srv"}, {"name": "run"})
    c2 = ToolCandidate("mcp__my_srv__run", "server 'my_srv' tool 'run'", {"name": "my_srv"}, {"name": "run"})
    c3 = ToolCandidate("mcp__other__ok", "server 'other' tool 'ok'", {"name": "other"}, {"name": "ok"})
    survivors = resolve_name_collisions([c1, c2, c3])
    assert [c.registry_name for c in survivors] == ["mcp__other__ok"]


def test_invisible_unicode_stripped_from_free_text_and_contract_kept():
    tag = "\U000E0041"
    bidi = "\u202E"
    spec = {
        "name": "do_thing",
        "description": f"Run{tag} {bidi}thing",
        "inputSchema": {
            "type": "object",
            "properties": {
                f"bad{tag}key": {
                    "type": "string",
                    "description": f"Arg{tag} desc",
                    "enum": [f"val{tag}1"],
                },
            },
            "required": [f"bad{tag}key"],
        },
    }
    converted = convert_mcp_schema("srv", spec)
    assert converted["description"] == "Run thing"
    dispatch_props = converted["dispatch_schema"]["properties"]
    assert f"bad{tag}key" in dispatch_props
    assert dispatch_props[f"bad{tag}key"]["description"] == "Arg desc"
    assert dispatch_props[f"bad{tag}key"]["enum"] == [f"val{tag}1"]

    wire_props = converted["parameters"]["properties"]
    assert len(wire_props) == 1
    wire_key = next(iter(wire_props))
    assert re.match(r"^[a-zA-Z0-9_.-]{1,64}$", wire_key)


def test_clean_schema_returned_as_same_object():
    clean = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Clean"}},
        "required": ["path"],
    }
    assert strip_schema_annotations(clean, "t") is clean


def test_inline_local_refs_pydantic_defs_and_definitions_and_path():
    schema = {
        "type": "object",
        "$defs": {
            "Loc": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            }
        },
        "definitions": {
            "Tag": {"type": "string"},
        },
        "properties": {
            "a": {"type": "integer"},
            "where": {"anyOf": [{"$ref": "#/$defs/Loc"}, {"type": "null"}]},
            "tag": {"$ref": "#/definitions/Tag"},
            "copy_a": {"$ref": "#/properties/a"},
            "defaulted": {"type": "object", "default": {"$ref": "#/not/a/real/ref"}},
        },
    }
    normalized = normalize_mcp_input_schema(schema)
    inlined = inline_local_refs(normalized)
    assert inlined["properties"]["where"]["type"] == "object"
    assert inlined["properties"]["where"]["properties"]["city"] == {"type": "string"}
    assert inlined["properties"]["tag"] == {"type": "string"}
    assert inlined["properties"]["copy_a"] == {"type": "integer"}
    assert inlined["properties"]["defaulted"]["default"] == {"$ref": "#/not/a/real/ref"}


@pytest.mark.parametrize(
    "bad_schema",
    [
        {
            "type": "object",
            "$defs": {"Node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}}}},
            "properties": {"root": {"$ref": "#/$defs/Node"}},
        },
        {
            "type": "object",
            "properties": {"ext": {"$ref": "https://example.com/schema.json"}},
        },
        {
            "type": "object",
            "properties": {"missing": {"$ref": "#/$defs/DoesNotExist"}},
        },
    ],
)
def test_inline_local_refs_rejects_recursive_remote_dangling(bad_schema):
    with pytest.raises(ValueError):
        inline_local_refs(bad_schema)


def test_inline_local_refs_rejects_diamond_budget():
    defs: dict[str, dict] = {"L0": {"type": "string"}}
    for i in range(1, 21):
        prev = f"#/$defs/L{i - 1}"
        defs[f"L{i}"] = {"type": "object", "properties": {"a": {"$ref": prev}, "b": {"$ref": prev}}}
    schema = {"type": "object", "$defs": defs, "properties": {"top": {"$ref": "#/$defs/L20"}}}
    with pytest.raises(ValueError, match="too large"):
        inline_local_refs(schema)


def test_rename_round_trip_through_ref_and_array_items():
    spec = {
        "name": "upload",
        "description": "Upload files",
        "inputSchema": {
            "type": "object",
            "$defs": {
                "Entry": {
                    "type": "object",
                    "properties": {"file path": {"type": "string"}},
                    "required": ["file path"],
                }
            },
            "properties": {
                "primary": {"$ref": "#/$defs/Entry"},
                "extras": {"type": "array", "items": {"$ref": "#/$defs/Entry"}},
            },
            "required": ["primary"],
        },
    }
    converted = convert_mcp_schema("srv", spec)
    wire_primary_props = converted["parameters"]["properties"]["primary"]["properties"]
    assert "file_path" in wire_primary_props
    assert "file path" not in wire_primary_props
    wire_items_props = converted["parameters"]["properties"]["extras"]["items"]["properties"]
    assert "file_path" in wire_items_props

    restored = unrename_tool_args(
        converted["dispatch_schema"],
        {"primary": {"file_path": "/a"}, "extras": [{"file_path": "/b"}]},
    )
    assert restored == {"primary": {"file path": "/a"}, "extras": [{"file path": "/b"}]}


def test_mcp_prefixed_tool_name_patterns():
    name = mcp_prefixed_tool_name("my-home server", "list.files")
    assert name == "mcp__my_home_server__list_files"
    assert name.isidentifier()
    assert re.match(r"^[a-zA-Z0-9_-]{1,64}$", name)
    assert re.match(r"^[a-zA-Z_][a-zA-Z0-9_.-]{0,63}$", name)


@pytest.mark.parametrize(
    ("bad_prop", "match"),
    [
        pytest.param({"allOf": [{"type": "object", "properties": {"a": {"type": "string"}}}]}, None, id="allOf"),
        pytest.param({"description": "anything"}, None, id="typeless"),
        pytest.param({"type": "array"}, None, id="array_without_items"),
        pytest.param({"type": "object", "properties": {"nested": {"type": "array"}}}, None, id="nested_array_without_items"),
        pytest.param({"anyOf": [{"description": "a"}, {"description": "b"}]}, "no branch", id="no_sendable_branch"),
    ],
)
def test_convert_mcp_schema_rejects_unconvertible_shapes(bad_prop, match):
    spec = {
        "name": "bad_tool",
        "inputSchema": {"type": "object", "properties": {"arg": bad_prop}},
    }
    with pytest.raises(ValueError, match=match):
        convert_mcp_schema("srv", spec)


def test_convert_mcp_schema_accepts_scalar_anyof_and_array_with_items():
    spec = {
        "name": "good_tool",
        "inputSchema": {
            "type": "object",
            "properties": {
                "val": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["tags"],
        },
    }
    out = convert_mcp_schema("srv", spec)
    assert out["parameters"]["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert out["parameters"]["properties"]["val"] == {"type": ["string", "integer"]}


@pytest.mark.parametrize(
    ("prop", "expected"),
    [
        pytest.param(
            {"anyOf": [{"type": "array"}, {"type": "string"}]},
            {"type": "string"},
            id="skip_itemless_array_branch",
        ),
        pytest.param(
            {"type": "string", "anyOf": [{"minLength": 3}, {"pattern": "^x"}]},
            {"type": "string", "minLength": 3},
            id="outer_type_fills_typeless_branch",
        ),
        pytest.param(
            {"anyOf": [{"type": "string", "enum": ["a", "b"]}, {"type": "integer"}]},
            {"type": "string", "enum": ["a", "b"]},
            id="enum_branch_not_loosened",
        ),
        pytest.param(
            {
                "type": "object",
                "properties": {"url": {"type": "string"}, "path": {"type": "string"}},
                "oneOf": [{"required": ["url"]}, {"required": ["path"]}],
            },
            None,
            id="outer_required_must_not_loosen_oneof_required",
        ),
    ],
)
def test_convert_mcp_schema_union_fallbacks(prop, expected):
    spec = {
        "name": "fallback_tool",
        "inputSchema": {"type": "object", "properties": {"arg": prop}},
    }
    if expected is None:
        with pytest.raises(ValueError, match="no branch"):
            convert_mcp_schema("srv", spec)
        return
    out = convert_mcp_schema("srv", spec)
    assert out["parameters"]["properties"]["arg"] == expected


def test_ref_merged_required_is_pruned():
    spec = {
        "name": "ref_tool",
        "inputSchema": {
            "type": "object",
            "properties": {
                "p": {"$ref": "#/$defs/Base", "type": "object", "properties": {"x": {"type": "string"}}},
            },
            "$defs": {"Base": {"required": ["y"]}},
        },
    }
    out = convert_mcp_schema("srv", spec)
    assert out["parameters"]["properties"]["p"]["required"] == []


def test_literal_data_reaches_the_wire_unchanged():
    schema_shaped = {"type": "object", "required": ["q"]}
    const_union = {"anyOf": [{"const": "a"}, {"const": "b"}]}
    null_union = {"anyOf": [{"type": "null"}, {"type": "string"}]}
    spec = {
        "name": "literal_tool",
        "inputSchema": {
            "type": "object",
            "properties": {
                "obj_default": {"type": "object", "properties": {}, "default": copy.deepcopy(schema_shaped)},
                "obj_examples": {"type": "object", "properties": {}, "examples": [copy.deepcopy(schema_shaped)]},
                "obj_enum": {"type": "object", "properties": {}, "enum": [copy.deepcopy(schema_shaped)]},
                "obj_const": {"type": "object", "properties": {}, "const": copy.deepcopy(schema_shaped)},
                "str_const_default": {"type": "string", "default": copy.deepcopy(const_union)},
                "str_null_default": {"type": "string", "default": copy.deepcopy(null_union)},
                "default": {"type": "object"},
            },
        },
    }
    out = convert_mcp_schema("srv", spec)
    props = out["parameters"]["properties"]
    assert props["obj_default"]["default"] == schema_shaped
    assert props["obj_examples"]["examples"] == [schema_shaped]
    assert props["obj_enum"]["enum"] == [schema_shaped]
    assert props["obj_const"]["const"] == schema_shaped
    assert props["str_const_default"]["default"] == const_union
    assert props["str_null_default"]["default"] == null_union
    assert props["default"] == {"type": "object", "properties": {}, "required": []}


def test_rename_round_trip_through_unions_and_nested_arrays():
    spec = {
        "name": "union_tool",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "anyOf": [
                        {"type": "object", "properties": {"file path": {"type": "string"}}},
                        {"type": "string"},
                    ]
                },
                "items_union": {
                    "type": "array",
                    "items": {
                        "oneOf": [
                            {"type": "object", "properties": {"file path": {"type": "string"}}},
                            {"type": "integer"},
                        ]
                    },
                },
                "matrix": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"file path": {"type": "string"}}},
                    },
                },
                "extra": {
                    "type": "object",
                    "additionalProperties": {"type": "object", "properties": {"file path": {"type": "string"}}},
                },
            },
        },
    }
    out = convert_mcp_schema("srv", spec)
    dispatch = out["dispatch_schema"]
    # Merged object|string union on wire has file_path in properties.
    assert "file_path" in out["parameters"]["properties"]["target"]["properties"]

    mapped_obj = unrename_tool_args(
        dispatch,
        {
            "target": {"file_path": "/a"},
            "items_union": [{"file_path": "/b"}, 3],
            "matrix": [[{"file_path": "/c"}]],
            "extra": {"k": {"file_path": "/d"}},
        },
    )
    assert mapped_obj == {
        "target": {"file path": "/a"},
        "items_union": [{"file path": "/b"}, 3],
        "matrix": [[{"file path": "/c"}]],
        "extra": {"k": {"file path": "/d"}},
    }
    assert unrename_tool_args(dispatch, {"target": "plain-string"}) == {"target": "plain-string"}


def test_rename_picks_the_union_branch_whose_const_matches():
    spec = {
        "name": "branch_tool",
        "inputSchema": {
            "type": "object",
            "properties": {
                "payload": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "const": "a"},
                                "file path": {"type": "string"},
                                "config": {"type": "object", "properties": {"opt one": {"type": "string"}}},
                            },
                        },
                        {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "const": "b"},
                                "file_path": {"type": "string"},
                                "config": {"type": "object", "properties": {"opt two": {"type": "string"}}},
                            },
                        },
                    ]
                }
            },
        },
    }
    out = convert_mcp_schema("srv", spec)
    dispatch = out["dispatch_schema"]

    mapped_a = unrename_tool_args(dispatch, {"payload": {"kind": "a", "file_path": "/x", "config": {"opt_one": "1"}}})
    assert mapped_a == {"payload": {"kind": "a", "file path": "/x", "config": {"opt one": "1"}}}

    mapped_b = unrename_tool_args(dispatch, {"payload": {"kind": "b", "file_path": "/y", "config": {"opt_two": "2"}}})
    assert mapped_b == {"payload": {"kind": "b", "file_path": "/y", "config": {"opt two": "2"}}}


def test_rename_prefers_branch_declaring_more_args_and_never_collapses_keys():
    # Case 1: both {"file path"} and {"file path", "file_path"} are valid branches;
    # sending both wire keys must keep both distinct instead of collapsing onto "file path".
    both_spellings_schema = {
        "type": "array",
        "items": {
            "oneOf": [
                {"type": "object", "properties": {"file path": {"type": "string"}}},
                {"type": "object", "properties": {"file path": {"type": "string"}, "file_path": {"type": "string"}}},
            ]
        },
    }
    assert unrename_tool_args(both_spellings_schema, [{"file_path": "x", "file_path_2": "y"}]) == [
        {"file_path": "x", "file path": "y"}
    ]

    # Case 2: rejected outer branch's nested child must not outrank a branch that declares all args.
    nested_vs_direct_schema = {
        "type": "array",
        "items": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {"kind": {"type": "string", "const": "a"}},
                    "oneOf": [
                        {"type": "object", "properties": {"file path": {"type": "string"}}},
                        {"type": "object", "properties": {"n": {"type": "integer"}}},
                    ],
                },
                {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "const": "b"},
                        "file_path": {"type": "string"},
                    },
                },
            ]
        },
    }
    assert unrename_tool_args(nested_vs_direct_schema, [{"kind": "b", "file_path": "x"}]) == [
        {"kind": "b", "file_path": "x"}
    ]


def test_renames_map_back_in_prefix_items_pattern_properties_and_conditionals():
    spec = {
        "name": "complex_tool",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tuple_arg": {
                    "type": "array",
                    "prefixItems": [
                        {"type": "object", "properties": {"first key": {"type": "string"}}},
                    ],
                    "items": {"type": "object", "properties": {"rest key": {"type": "string"}}},
                },
                "bag": {
                    "type": "object",
                    "patternProperties": {
                        "^x_": {"type": "object", "properties": {"pat key": {"type": "string"}}},
                    },
                },
                "cond": {
                    "type": "object",
                    "properties": {"mode": {"type": "string"}},
                    "if": {"properties": {"mode": {"const": "full"}}},
                    "then": {"properties": {"then key": {"type": "string"}}},
                    "else": {"properties": {"else key": {"type": "string"}}},
                },
            },
        },
    }
    dispatch = convert_mcp_schema("srv", spec)["dispatch_schema"]
    mapped = unrename_tool_args(
        dispatch,
        {
            "tuple_arg": [{"first_key": "1"}, {"rest_key": "2"}],
            "bag": {"x_1": {"pat_key": "3"}},
            "cond": {"mode": "full", "then_key": "4", "else_key": "5"},
        },
    )
    assert mapped == {
        "tuple_arg": [{"first key": "1"}, {"rest key": "2"}],
        "bag": {"x_1": {"pat key": "3"}},
        "cond": {"mode": "full", "then key": "4", "else key": "5"},
    }


def test_conditional_rename_follows_the_matching_if():
    all_of_schema = {
        "type": "object",
        "allOf": [
            {
                "if": {"properties": {"kind": {"type": "string", "const": "a"}}},
                "then": {"properties": {"file path": {"type": "string"}}},
            },
            {
                "if": {"properties": {"kind": {"type": "string", "const": "b"}}},
                "then": {"properties": {"file_path": {"type": "string"}}},
            },
        ],
    }
    assert unrename_tool_args(all_of_schema, {"kind": "a", "file_path": "/a"}) == {"kind": "a", "file path": "/a"}
    assert unrename_tool_args(all_of_schema, {"kind": "b", "file_path": "/b"}) == {"kind": "b", "file_path": "/b"}

    if_else_schema = {
        "type": "object",
        "if": {"properties": {"kind": {"type": "string", "const": "a"}}},
        "then": {"properties": {"file path": {"type": "string"}}},
        "else": {"properties": {"file_path": {"type": "string"}}},
    }
    assert unrename_tool_args(if_else_schema, {"kind": "a", "file_path": "/a"}) == {"kind": "a", "file path": "/a"}
    assert unrename_tool_args(if_else_schema, {"kind": "b", "file_path": "/b"}) == {"kind": "b", "file_path": "/b"}

    redeclared_discriminator_schema = {
        "type": "object",
        "allOf": [
            {
                "if": {"properties": {"kind": {"type": "string", "const": "a"}}},
                "then": {"properties": {"kind": {"type": "string"}, "file path": {"type": "string"}}},
            },
            {
                "if": {"properties": {"kind": {"type": "string", "const": "b"}}},
                "then": {"properties": {"kind": {"type": "string"}, "file_path": {"type": "string"}}},
            },
        ],
    }
    assert unrename_tool_args(redeclared_discriminator_schema, {"kind": "a", "file_path": "/a"}) == {
        "kind": "a",
        "file path": "/a",
    }
    assert unrename_tool_args(redeclared_discriminator_schema, {"kind": "b", "file_path": "/b"}) == {
        "kind": "b",
        "file_path": "/b",
    }


def test_pattern_properties_regex_is_not_run():
    schema = {
        "type": "object",
        "properties": {
            "bag": {
                "type": "object",
                "patternProperties": {
                    "(": {"type": "object", "properties": {"nested key": {"type": "string"}}},
                },
            }
        },
    }
    assert unrename_tool_args(schema, {"bag": {"anything": {"nested_key": "v"}}}) == {
        "bag": {"anything": {"nested key": "v"}}
    }
