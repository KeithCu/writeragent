import json

import pytest

from plugin.framework.tool import ToolBase, to_mcp_schema, to_openai_schema
from plugin.framework.tool_schema import _normalize_schema_for_strict_providers

class DummyTool(ToolBase):
    name = "dummy_tool"
    description = "A simple tool"
    parameters = {
        "properties": {
            "arg1": {
                "type": "string",
                "description": "argument 1"
            }
        },
        "required": ["arg1"]
    }
    def execute(self, ctx, **kwargs):
        pass

class ToolNoParams(ToolBase):
    name = "no_params"
    description = "A tool with no parameters"
    def execute(self, ctx, **kwargs):
        pass

def test_to_openai_schema():
    tool = DummyTool()
    schema = to_openai_schema(tool)

    assert schema["type"] == "function"
    assert schema["function"]["name"] == "dummy_tool"
    assert schema["function"]["description"] == "A simple tool"
    assert schema["function"]["parameters"]["type"] == "object"
    assert "arg1" in schema["function"]["parameters"]["properties"]
    assert schema["function"]["parameters"]["properties"]["arg1"]["type"] == "string"
    assert "arg1" in schema["function"]["parameters"]["required"]

def test_to_openai_schema_no_params():
    tool = ToolNoParams()
    schema = to_openai_schema(tool)

    assert schema["type"] == "function"
    assert schema["function"]["name"] == "no_params"
    assert schema["function"]["parameters"]["type"] == "object"

def test_to_mcp_schema():
    tool = DummyTool()
    schema = to_mcp_schema(tool)

    assert schema["name"] == "dummy_tool"
    assert schema["description"] == "A simple tool"
    assert schema["inputSchema"]["type"] == "object"
    assert "arg1" in schema["inputSchema"]["properties"]
    assert "document_url" in schema["inputSchema"]["properties"]
    assert "arg1" in schema["inputSchema"]["required"]

def test_to_mcp_schema_no_params():
    tool = ToolNoParams()
    schema = to_mcp_schema(tool)

    assert schema["name"] == "no_params"
    assert schema["inputSchema"]["type"] == "object"

def test_normalize_schema_union_type():
    """A union stays as wide as validate. array does not erase string."""
    params = {"type": ["string", "array"], "items": {"type": "string"}}
    res = _normalize_schema_for_strict_providers(params)
    assert res["type"] == ["string", "array"]
    assert res["items"]["type"] == "string"

    params = {"type": ["number", "string"]}
    res = _normalize_schema_for_strict_providers(params)
    assert res["type"] == ["number", "string"]

    # Not an array: stray items still go. Null on a scalar union is kept.
    params = {"type": ["string", "number", "null"], "items": {"type": "integer"}}
    res = _normalize_schema_for_strict_providers(params)
    assert res["type"] == ["string", "number", "null"]
    assert "items" not in res

def test_normalize_schema_empty_required():
    params = {"type": "object", "required": []}
    res = _normalize_schema_for_strict_providers(params)
    assert "required" not in res

def test_normalize_schema_nested_properties():
    params = {
        "type": "object",
        "properties": {
            "p1": {"type": ["string", "null"]},
            "p2": {
                "type": "object",
                "required": []
            }
        }
    }
    res = _normalize_schema_for_strict_providers(params)
    assert res["properties"]["p1"]["type"] == ["string", "null"]
    assert "required" not in res["properties"]["p2"]

def test_normalize_schema_items():
    params = {
        "type": "array",
        "items": {"type": ["string", "integer"]}
    }
    res = _normalize_schema_for_strict_providers(params)
    assert res["items"]["type"] == ["string", "integer"]


def test_normalize_schema_not_array_remove_items():
    params = {"type": "string", "items": {"type": "string"}}
    res = _normalize_schema_for_strict_providers(params)
    assert "items" not in res


def test_normalize_schema_implicit_object_and_array():
    """properties without type is still an object. items without type is still an array."""
    params = {
        "properties": {
            "inner": {
                "properties": {"n": {"type": "integer"}},
            },
            "value": {"type": ["string", "number"]},
        },
        "required": ["value"],
    }
    res = _normalize_schema_for_strict_providers(params)
    assert res["properties"]["inner"]["properties"]["n"]["type"] == ["integer", "null"]
    # Required scalar union stays both types and does not gain null.
    assert res["properties"]["value"]["type"] == ["string", "number"]

    implicit_array = {"items": {"type": ["string", "integer"]}}
    res = _normalize_schema_for_strict_providers(implicit_array)
    assert res["items"]["type"] == ["string", "integer"]

    array_null = {"type": ["array", "null"], "items": {"type": "string"}}
    res = _normalize_schema_for_strict_providers(array_null)
    assert res["type"] == ["array", "null"]
    assert res["items"]["type"] == "string"


def test_optional_scalar_union_keeps_members_and_null():
    params = {
        "type": "object",
        "properties": {"value": {"type": ["string", "number"]}},
    }
    res = _normalize_schema_for_strict_providers(params)
    assert res["properties"]["value"]["type"] == ["string", "number", "null"]


def test_apply_document_content_keeps_array_or_string():
    """OpenAI must not advertise content as array-only; execute accepts a string."""
    from plugin.writer.apply_document_content import ApplyDocumentContent

    content = to_openai_schema(ApplyDocumentContent())["function"]["parameters"]["properties"]["content"]
    assert content["type"] == ["array", "string"]
    assert content["items"]["type"] == "string"
    mcp_content = to_mcp_schema(ApplyDocumentContent())["inputSchema"]["properties"]["content"]
    assert mcp_content["type"] == ["array", "string"]
    assert mcp_content["items"]["type"] == "string"

def test_normalize_schema_none_dict():
    assert _normalize_schema_for_strict_providers(None) is None
    assert _normalize_schema_for_strict_providers("string") == "string"


def test_normalize_schema_optional_scalar_gets_null():
    params = {
        "type": "object",
        "properties": {
            "max_chars": {"type": "integer", "description": "limit"},
            "scope": {"type": "string", "enum": ["full", "selection"]},
        },
    }
    res = _normalize_schema_for_strict_providers(params)
    assert res["properties"]["max_chars"]["type"] == ["integer", "null"]
    assert res["properties"]["scope"]["type"] == ["string", "null"]
    assert res["properties"]["scope"]["enum"] == ["full", "selection", None]


def test_normalize_schema_required_scalar_stays_non_nullable():
    params = {
        "type": "object",
        "properties": {"index": {"type": "integer"}},
        "required": ["index"],
    }
    res = _normalize_schema_for_strict_providers(params)
    assert res["properties"]["index"]["type"] == "integer"


@pytest.mark.parametrize(
    "schema_fn,scope_type,scope_enum",
    [
        ("openai", ["string", "null"], lambda e: None in e and "full" in e),
        ("mcp", "string", lambda e: None not in e and "full" in e),
    ],
)
def test_optional_enum_nullable_openai_vs_mcp(schema_fn, scope_type, scope_enum):
    from plugin.writer.get_document_content import GetDocumentContent

    tool = GetDocumentContent()
    if schema_fn == "openai":
        props = to_openai_schema(tool)["function"]["parameters"]["properties"]
        assert props["include_images"]["type"] == ["boolean", "null"]
        assert props["start"]["type"] == ["integer", "null"]
    else:
        props = to_mcp_schema(tool)["inputSchema"]["properties"]
        assert props["document_url"]["type"] == ["string", "null"]
    assert props["scope"]["type"] == scope_type
    assert scope_enum(props["scope"].get("enum", []))
    assert props["max_chars"]["type"] == ["integer", "null"]


def test_mcp_optional_enum_nested_object():
    params = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {
                    "inner_scope": {"type": "string", "enum": ["a", "b"]},
                },
            },
        },
    }
    mcp = _normalize_schema_for_strict_providers(params, for_mcp=True)
    inner = mcp["properties"]["outer"]["properties"]["inner_scope"]
    assert inner["type"] == "string"
    assert inner["enum"] == ["a", "b"]
    assert None not in inner["enum"]


def test_mcp_writer_optional_enums_no_null_in_enum():
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.get_document_content import GetDocumentContent
    from plugin.writer.outline import GetDocumentTree
    from plugin.writer.styles import ApplyStyle, StyleFormatText

    def assert_mcp_string_enum(props, key):
        prop = props[key]
        assert prop["type"] == "string", key
        assert None not in prop.get("enum", []), key

    adc = to_mcp_schema(ApplyDocumentContent())["inputSchema"]["properties"]
    assert_mcp_string_enum(adc, "position")
    assert_mcp_string_enum(adc, "target")
    sty = to_mcp_schema(ApplyStyle())["inputSchema"]["properties"]
    assert_mcp_string_enum(sty, "target")
    assert_mcp_string_enum(sty, "clear_direct")
    assert_mcp_string_enum(sty, "family")
    sft = to_mcp_schema(StyleFormatText())["inputSchema"]["properties"]
    assert_mcp_string_enum(sft, "target")
    gdc = to_mcp_schema(GetDocumentContent())["inputSchema"]["properties"]
    assert_mcp_string_enum(gdc, "scope")
    gdt = to_mcp_schema(GetDocumentTree())["inputSchema"]["properties"]
    assert_mcp_string_enum(gdt, "strategy")


def test_to_mcp_schema_delegate_writer_flattens_domain_description():
    from plugin.doc.specialized_base import DelegateToSubagent

    tool = DelegateToSubagent()
    mcp_schema = to_mcp_schema(tool)

    assert "\n" not in mcp_schema["description"]
    domain_desc = mcp_schema["inputSchema"]["properties"]["domain"]["description"]
    assert "\n" not in domain_desc
    assert domain_desc != "The specialized domain to activate."
    domain_enum = mcp_schema["inputSchema"]["properties"]["domain"]["enum"]
    assert "brainstorming" not in domain_enum
    assert "writing_plan" not in domain_enum


def test_to_mcp_schema_delegate_calc_domain_list_omits_analysis_and_python():
    from plugin.doc.specialized_base import DelegateToSubagent

    mcp_schema = to_mcp_schema(DelegateToSubagent(), doc_type="calc")
    domain_enum = mcp_schema["inputSchema"]["properties"]["domain"]["enum"]
    assert "python" not in domain_enum
    assert "analysis" not in domain_enum
    assert "solvers" not in domain_enum


def test_update_style_schema_emits_no_additional_properties_keyword():
    """xAI/OpenRouter reject nested additionalProperties; StyleUpdate uses exhaustive properties only."""
    from plugin.writer.styles import StyleUpdate

    schema = to_openai_schema(StyleUpdate())
    wire = json.dumps(schema["function"]["parameters"])
    assert "additionalProperties" not in wire
    assert "property_updates" in schema["function"]["parameters"]["properties"]


def test_empty_properties_strips_kwargs_before_any_provider():
    """properties {} is a no-arg schema. Extra kwargs must not survive the one pre-shim check."""
    from plugin.framework.tool_schema import normalize_outbound_tool_calls

    tools = [{
        "type": "function",
        "function": {
            "name": "sheet_list",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    messages = [{
        "role": "assistant",
        "tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {"name": "sheet_list", "arguments": '{"hallucinated": "yes"}'},
        }],
    }]
    out = normalize_outbound_tool_calls(messages, tools)
    args = json.loads(out[0]["tool_calls"][0]["function"]["arguments"])
    assert args == {}


def test_normalize_outbound_accepts_tuple_messages_and_tools():
    """A tuple used to miss the list check and leave hallucinated kwargs in place."""
    from plugin.framework.tool_schema import normalize_outbound_tool_calls

    tools = ({
        "type": "function",
        "function": {
            "name": "sheet_list",
            "parameters": {"type": "object", "properties": {}},
        },
    },)
    messages = ({
        "role": "assistant",
        "tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {"name": "sheet_list", "arguments": '{"hallucinated": "yes"}'},
        }],
    },)
    out = normalize_outbound_tool_calls(messages, tools)
    args = json.loads(out[0]["tool_calls"][0]["function"]["arguments"])
    assert args == {}
    # prepare_chat_messages already copies; this helper mutates that copy.
    assert out is messages


def test_duplicate_tool_schemas_union_properties_not_last_wins():
    """A later empty properties object must not erase an earlier schema for the same name."""
    from plugin.framework.tool_schema import normalize_outbound_tool_calls

    tools = [
        {
            "type": "function",
            "function": {
                "name": "shape_upsert",
                "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "shape_upsert",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]
    messages = [{
        "role": "assistant",
        "tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {"name": "shape_upsert", "arguments": '{"action": "edit", "hallucinated": 1}'},
        }],
    }]
    out = normalize_outbound_tool_calls(messages, tools)
    args = json.loads(out[0]["tool_calls"][0]["function"]["arguments"])
    assert args == {"action": "edit"}


def test_coerce_call_args_wraps_array_range_and_formula_values():
    from plugin.framework.tool_schema import coerce_call_args

    props = {
        "range": {"type": "array", "items": {"type": "string"}},
        "values": {
            "type": ["string", "array", "null"],
            "items": {
                "type": ["array", "string", "number", "boolean", "null"],
                "items": {"type": ["string", "number", "boolean", "null"]},
            },
        },
    }
    out = coerce_call_args(props, {"range": "A1", "values": ["a", 1]})
    assert out["range"] == ["A1"]
    assert out["values"] == ["a", 1]
    empty = coerce_call_args(props, {"range": "A1", "values": []})
    assert empty["values"] == []
    number = coerce_call_args(props, {"range": "A1", "values": 3})
    assert number["values"] == "3"
    plain = coerce_call_args({"range": {"type": "string"}}, {"range": "A1"})
    assert plain["range"] == "A1"


def test_write_formula_range_values_schema_string_array_union():
    """Chat and MCP share the source values union (same guard as apply_content)."""
    from plugin.calc.cells import WriteCellRange

    tool = WriteCellRange()
    openai = to_openai_schema(tool)["function"]["parameters"]["properties"]["values"]
    mcp = to_mcp_schema(tool)["inputSchema"]["properties"]["values"]
    item_types = ["array", "string", "number", "boolean", "null"]
    for schema in (openai, mcp):
        assert schema["type"] == ["string", "array", "null"]
        assert schema["items"]["type"] == item_types
        assert schema["items"]["items"]["type"] == ["string", "number", "boolean", "null"]
    dumped = json.dumps(to_openai_schema(tool))
    assert "anyOf" not in dumped
    assert "oneOf" not in dumped
    openai_params = to_openai_schema(tool)["function"]["parameters"]
    assert "source" in openai_params["properties"]
    assert openai_params["required"] == ["range"]


def test_mcp_widens_array_range_to_string_or_array():
    """MCP hosts reject a bare range string when schema is array-only; execute already coerces."""
    from plugin.calc.cells import ReadCellRange, WriteCellRange

    for tool in (WriteCellRange(), ReadCellRange()):
        openai = to_openai_schema(tool)["function"]["parameters"]["properties"]["range"]
        mcp = to_mcp_schema(tool)["inputSchema"]["properties"]["range"]
        assert openai["type"] == "array"
        assert mcp["type"] == ["string", "array"]
        assert mcp["items"]["type"] == "string"


def test_mcp_string_range_stays_string():
    """Nested / non-top-level string range must not gain an array union."""
    from plugin.calc.duckdb_tools import QueryFolderSqlTool

    tool = QueryFolderSqlTool()
    tables = to_mcp_schema(tool)["inputSchema"]["properties"]["tables"]
    nested = tables["additionalProperties"]["properties"]["range"]
    openai_tables = to_openai_schema(tool)["function"]["parameters"]["properties"]["tables"]
    openai_nested = openai_tables["additionalProperties"]["properties"]["range"]
    assert openai_nested["type"] == "string"
    assert nested["type"] == "string"


def test_conditional_formatting_list_range_is_array_mcp_widens():
    """conditional_formatting_list uses the same array range schema as other Calc tools."""
    from plugin.calc.conditional import ListConditionalFormats

    tool = ListConditionalFormats()
    openai = to_openai_schema(tool)["function"]["parameters"]["properties"]["range"]
    mcp = to_mcp_schema(tool)["inputSchema"]["properties"]["range"]
    assert openai["type"] == "array"
    assert mcp["type"] == ["string", "array"]
    assert mcp["items"]["type"] == "string"


def test_required_enum_does_not_gain_null():
    params = {
        "type": "object",
        "properties": {"mode": {"type": "string", "enum": ["a", "b"]}},
        "required": ["mode"],
    }
    res = _normalize_schema_for_strict_providers(params)
    assert res["properties"]["mode"]["type"] == "string"
    assert res["properties"]["mode"]["enum"] == ["a", "b"]


def test_named_range_flags_schema_is_string_array_not_oneof():
    from plugin.calc.named_ranges import NamedRangeAdd, NamedRangeEdit

    for tool in (NamedRangeAdd(), NamedRangeEdit()):
        flags = to_openai_schema(tool)["function"]["parameters"]["properties"]["flags"]
        assert "oneOf" not in flags
        assert "anyOf" not in flags
        assert flags["type"] == "array"
        assert flags["items"]["type"] == "string"
        assert isinstance(flags["items"]["enum"], list) and flags["items"]["enum"]


def test_venv_data_range_schema_is_string_not_oneof():
    from plugin.calc.python.venv import RunVenvPythonScript

    tool = RunVenvPythonScript()
    for doc_type in ("calc", None):
        props = to_openai_schema(tool, doc_type=doc_type)["function"]["parameters"]["properties"]
        dr = props["data_range"]
        assert "oneOf" not in dr
        assert dr["type"] == ["string", "null"]


def test_query_folder_sql_files_schema_is_object():
    from plugin.calc.duckdb_tools import QueryFolderSqlTool

    files = to_openai_schema(QueryFolderSqlTool())["function"]["parameters"]["properties"]["files"]
    assert files["type"] == "object"
    assert files["additionalProperties"]["type"] == "string"


def test_normalize_schema_non_list_required():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": "ab",
    }
    normalized = _normalize_schema_for_strict_providers(schema)
    assert normalized["properties"]["a"]["type"] == ["string", "null"]
    assert normalized["required"] == "ab"


def test_to_mcp_schema_injects_document_url():
    """Test that to_mcp_schema dynamically injects document_url parameter."""
    class DummyTool(ToolBase):
        name = "dummy_tool"
        description = "A dummy tool for testing schema injection."
        parameters = {
            "type": "object",
            "properties": {
                "arg1": {"type": "string"}
            },
            "required": ["arg1"]
        }
        def execute(self, ctx, **kwargs):
            return {"status": "ok"}

    schema = to_mcp_schema(DummyTool())
    input_schema = schema.get("inputSchema", {})
    assert "document_url" in input_schema.get("properties", {})
    assert input_schema["properties"]["document_url"]["type"] == ["string", "null"]


@pytest.mark.parametrize(
    ("schema_type", "raw", "expected"),
    [
        ("integer", "3", 3),
        ("integer", " -2 ", -2),
        ("integer", "3.0", 3),
        ("integer", 3.0, 3),
        ("integer", "3.5", "3.5"),
        ("integer", "1_0", "1_0"),
        ("integer", "１２", "１２"),
        ("integer", "abc", "abc"),
        ("number", "1.5", 1.5),
        ("number", "2", 2),
        ("number", "nan", "nan"),
        ("number", "1e999", "1e999"),
    ],
)
def test_coerce_numeric_strings(schema_type, raw, expected):
    from plugin.framework.tool_schema import coerce_call_args

    out = coerce_call_args({"n": {"type": schema_type}}, {"n": raw})
    assert out["n"] == expected
    assert type(out["n"]) is type(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TRUE", True),
        (" yes ", True),
        ("1", True),
        (1, True),
        ("false", False),
        ("no", False),
        ("0", False),
        (0, False),
        ("maybe", "maybe"),
        (2, 2),
    ],
)
def test_coerce_boolean_words(raw, expected):
    from plugin.framework.tool_schema import coerce_call_args

    out = coerce_call_args({"flag": {"type": "boolean"}}, {"flag": raw})
    assert out["flag"] is expected if isinstance(expected, bool) else out["flag"] == expected


def test_coerce_number_to_string():
    from plugin.framework.tool_schema import coerce_call_args

    assert coerce_call_args({"s": {"type": "string"}}, {"s": 50})["s"] == "50"
    assert coerce_call_args({"s": {"type": "string"}}, {"s": True})["s"] is True
    assert coerce_call_args({"s": {"type": ["integer", "string"]}}, {"s": "5"})["s"] == "5"
    assert coerce_call_args({"values": {"type": ["string", "array", "null"]}}, {"values": 3})["values"] == "3"


@pytest.mark.parametrize(
    ("schema_type", "raw", "expected"),
    [
        ("array", "A1", ["A1"]),
        ("array", '["A1:B2"]', ["A1:B2"]),
        ("array", 3, [3]),
        ("array", {"a": 1}, [{"a": 1}]),
        ("array", '{"field": 1}', [{"field": 1}]),
        ("array", "[not json", ["[not json"]),
        (["array", "string"], "a,b", "a,b"),
    ],
)
def test_coerce_single_value_to_list(schema_type, raw, expected):
    from plugin.framework.tool_schema import coerce_call_args

    out = coerce_call_args({"items": {"type": schema_type}}, {"items": raw})
    assert out["items"] == expected


def test_coerce_json_object_string():
    from plugin.framework.tool_schema import coerce_call_args

    assert coerce_call_args({"obj": {"type": "object"}}, {"obj": '{"a": 1}'})["obj"] == {"a": 1}
    assert coerce_call_args({"obj": {"type": "object"}}, {"obj": "x"})["obj"] == "x"
    assert coerce_call_args({"obj": {"type": "object"}}, {"obj": "[1]"})["obj"] == "[1]"


@pytest.mark.parametrize(
    ("prop", "raw", "expected"),
    [
        ({"type": "string", "enum": ["GREATER", "LESS"]}, "greater", "GREATER"),
        ({"type": "string", "enum": ["top_left", "bottom_right"]}, "Bottom Right", "bottom_right"),
        ({"type": "string", "enum": ["rows", "cols"]}, "row", "rows"),
        ({"type": "string", "enum": ["footnote", "endnote"]}, "Footnotes", "footnote"),
        ({"type": "string", "enum": ["left", "center", "right"]}, "centre", "center"),
        ({"type": "string", "enum": ["register", "unregister"]}, "registr", "register"),
        ({"type": "string", "enum": ["register", "unregister"]}, "unregistr", "unregister"),
        ({"type": "integer", "enum": [0, 1, 2]}, "1", 1),
    ],
)
def test_coerce_enum_spelling(prop, raw, expected):
    from plugin.framework.tool_schema import coerce_call_args

    assert coerce_call_args({"op": prop}, {"op": raw})["op"] == expected


def test_coerce_enum_leaves_ambiguous_or_short():
    from plugin.framework.tool_schema import coerce_call_args

    assert coerce_call_args({"k": {"type": "string", "enum": ["line", "pie"]}}, {"k": "lin"})["k"] == "lin"
    assert coerce_call_args({"k": {"type": "string", "enum": ["alpha1", "alpha2"]}}, {"k": "alpha3"})["k"] == "alpha3"
    assert coerce_call_args({"k": {"type": "string", "enum": ["ordered", "unordered"]}}, {"k": "deordered"})["k"] == "deordered"
    assert coerce_call_args({"k": {}}, {"k": "7"})["k"] == "7"


@pytest.mark.parametrize(
    ("prop", "raw", "expected"),
    [
        ({"type": "array", "items": {"type": "integer"}}, "3", [3]),
        ({"type": "array", "items": {"type": "integer"}}, '["3"]', [3]),
        ({"type": "array", "items": {"type": "integer"}}, 3.0, [3]),
        ({"type": "array", "items": {"type": "integer"}}, "x", ["x"]),
        ({"type": "array", "items": {"type": "integer"}}, ["3"], [3]),
        ({"type": "array", "items": {"type": "integer"}}, [True, None, "x", "3.5"], [True, None, "x", "3.5"]),
        ({"type": "array", "items": {"type": "number"}}, ["0", "1.5"], [0, 1.5]),
        ({"type": "array", "items": {"type": "string"}}, 5, ["5"]),
        ({"type": "array", "items": {"type": "string"}}, [2023], ["2023"]),
        ({"type": "array", "items": {"type": ["integer", "string"]}}, "3", ["3"]),
        ({"type": "array", "items": {"type": ["integer", "string"]}}, ["3", 255], ["3", 255]),
        ({"type": "array", "items": {"type": "array"}}, "x", ["x"]),
        ({"type": "array", "items": {"type": "array"}}, [["1"]], [["1"]]),
        ({"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}, [["1"], "x"], [[1], "x"]),
        ({"type": "array", "items": {"type": "string", "enum": ["print_area", "repeat_rows"]}}, ["Print Area"], ["print_area"]),
        ({"type": "array", "items": {"type": "object"}}, [{"a": "1"}], [{"a": "1"}]),
    ],
)
def test_coerce_converts_list_elements(prop, raw, expected):
    from plugin.framework.tool_schema import coerce_call_args

    snapshot = list(raw) if isinstance(raw, list) else raw
    out = coerce_call_args({"v": prop}, {"v": raw})
    assert out["v"] == expected
    assert [type(x) for x in out["v"]] == [type(x) for x in expected]
    if isinstance(raw, list):
        assert raw == snapshot


def test_coerce_keeps_array_string_union_literal():
    from plugin.framework.tool_schema import coerce_call_args

    prop = {"type": ["array", "string"], "items": {"type": "string"}}
    out = coerce_call_args({"v": prop}, {"v": '["a","b"]'})
    assert out["v"] == '["a","b"]'
    assert isinstance(out["v"], str)


def test_coerce_nested_objects_items_and_no_mutation():
    from plugin.framework.tool_schema import coerce_call_args

    props = {
        "then_by": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "integer"},
                    "ascending": {"type": "boolean"},
                },
            },
        },
        "connections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_end": {"type": "string", "enum": ["none", "arrow"]},
                },
            },
        },
    }
    raw_item = {"column": "0", "ascending": "false", "extra": "keep"}
    raw_then_by = [raw_item, '{"column": "1", "ascending": "true"}']
    raw_conns = [{"line_end": "Arrow"}]
    raw = {"then_by": raw_then_by, "connections": raw_conns}

    out = coerce_call_args(props, raw)
    assert out["then_by"] == [
        {"column": 0, "ascending": False, "extra": "keep"},
        {"column": 1, "ascending": True},
    ]
    assert type(out["then_by"][0]["column"]) is int
    assert type(out["then_by"][0]["ascending"]) is bool
    assert out["connections"] == [{"line_end": "arrow"}]
    assert raw_item == {"column": "0", "ascending": "false", "extra": "keep"}
    assert raw_then_by[1] == '{"column": "1", "ascending": "true"}'
    assert raw_conns == [{"line_end": "Arrow"}]


def test_coerce_additional_properties_maps():
    from plugin.framework.tool_schema import coerce_call_args

    raw_str_map = {"n": 5, "b": True}
    raw_obj_map = {"a": {"headers": "false"}, "b": "S.A1:B2"}
    props = {
        "str_map": {"type": "object", "additionalProperties": {"type": "string"}},
        "obj_map": {
            "type": ["object", "null"],
            "additionalProperties": {
                "type": "object",
                "properties": {"headers": {"type": "boolean"}},
            },
        },
        "declared_wins": {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "additionalProperties": {"type": "string"},
        },
        "bool_true": {"type": "object", "additionalProperties": True},
        "bool_false": {"type": "object", "additionalProperties": False},
        "pattern_map": {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "patternProperties": {"^x_": {"type": "integer"}},
            "additionalProperties": {"type": "string"},
        },
    }
    out = coerce_call_args(
        props,
        {
            "str_map": raw_str_map,
            "obj_map": raw_obj_map,
            "declared_wins": {"count": "7", "note": 9},
            "bool_true": {"n": 5},
            "bool_false": {"n": 5},
            "pattern_map": {"count": "3", "x_val": 4, "extra": 5},
        },
    )
    assert out["str_map"] == {"n": "5", "b": True}
    assert out["obj_map"] == {"a": {"headers": False}, "b": "S.A1:B2"}
    assert out["declared_wins"] == {"count": 7, "note": "9"}
    assert type(out["declared_wins"]["count"]) is int
    assert out["bool_true"] == {"n": 5}
    assert out["bool_false"] == {"n": 5}
    assert out["pattern_map"] == {"count": 3, "x_val": 4, "extra": 5}
    assert type(out["pattern_map"]["count"]) is int
    assert raw_str_map == {"n": 5, "b": True}
    assert raw_obj_map == {"a": {"headers": "false"}, "b": "S.A1:B2"}


def test_coerce_color_properties_keep_integer():
    from unittest.mock import MagicMock

    from plugin.framework.tool import ToolRegistry
    from plugin.framework.tool_schema import coerce_call_args

    registry = ToolRegistry(MagicMock())
    for pkg in ("plugin.calc", "plugin.draw", "plugin.writer", "plugin.writer.images", "plugin.writer.specialized"):
        registry.auto_discover_package(pkg)

    top_hits = 0
    nested_hits = 0
    list_hits = 0

    def _walk(spec: dict, depth: int) -> None:
        nonlocal top_hits, nested_hits, list_hits
        props = spec.get("properties")
        if isinstance(props, dict):
            for name, prop_spec in props.items():
                if not isinstance(prop_spec, dict):
                    continue
                lowered = name.casefold()
                if lowered.endswith("color"):
                    if depth == 0:
                        top_hits += 1
                    else:
                        nested_hits += 1
                    out = coerce_call_args({name: prop_spec}, {name: 255})
                    assert out[name] == 255 and type(out[name]) is int, f"{name} coerced 255 to {out[name]!r}"
                elif lowered.endswith("colors"):
                    list_hits += 1
                    out = coerce_call_args({name: prop_spec}, {name: [255]})
                    assert out[name] == [255] and type(out[name][0]) is int, f"{name} coerced [255] to {out[name]!r}"
                _walk(prop_spec, depth + 1)
        items = spec.get("items")
        if isinstance(items, dict):
            _walk(items, depth + 1)
        extra = spec.get("additionalProperties")
        if isinstance(extra, dict):
            _walk(extra, depth + 1)

    for tool in registry.get_tools(filter_doc_type=False, exclude_tiers=()):
        params = tool.parameters if isinstance(tool.parameters, dict) else {}
        if isinstance(params.get("properties"), dict):
            _walk(params, 0)

    assert top_hits >= 1
    assert nested_hits >= 1
    assert list_hits >= 1
