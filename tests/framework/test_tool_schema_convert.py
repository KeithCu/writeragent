import json
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.tool import ToolBase, _normalize_schema_for_strict_providers, to_mcp_schema, to_openai_schema

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
    assert "arg1" in schema["inputSchema"]["required"]

def test_to_mcp_schema_no_params():
    tool = ToolNoParams()
    schema = to_mcp_schema(tool)

    assert schema["name"] == "no_params"
    assert schema["inputSchema"]["type"] == "object"

def test_normalize_schema_union_type():
    params = {"type": ["string", "array"]}
    res = _normalize_schema_for_strict_providers(params)
    assert res["type"] == "array"

    params = {"type": ["number", "string"]}
    res = _normalize_schema_for_strict_providers(params)
    assert res["type"] == "number"

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
    assert res["items"]["type"] == "string"

    # Items as list
    params = {
        "type": "array",
        "items": [{"type": "string"}, {"type": "integer"}]
    }
    res = _normalize_schema_for_strict_providers(params)
    assert res["items"]["type"] == "string"

def test_normalize_schema_not_array_remove_items():
    params = {"type": "string", "items": {"type": "string"}}
    res = _normalize_schema_for_strict_providers(params)
    assert "items" not in res

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
    assert res["properties"]["scope"]["enum"] == ["full", "selection", "null"]


def test_normalize_schema_required_scalar_stays_non_nullable():
    params = {
        "type": "object",
        "properties": {"index": {"type": "integer"}},
        "required": ["index"],
    }
    res = _normalize_schema_for_strict_providers(params)
    assert res["properties"]["index"]["type"] == "integer"


def test_normalize_schema_scalar_null_union_preserved():
    params = {"type": ["integer", "null"]}
    res = _normalize_schema_for_strict_providers(params)
    assert res["type"] == ["integer", "null"]


def test_optional_integer_allows_null_on_openai_wire():
    from plugin.writer.content import GetDocumentContent

    props = to_openai_schema(GetDocumentContent())["function"]["parameters"]["properties"]
    assert props["max_chars"]["type"] == ["integer", "null"]
    assert props["include_images"]["type"] == ["boolean", "null"]
    assert props["start"]["type"] == ["integer", "null"]
    assert props["scope"]["type"] == ["string", "null"]
    assert "null" in props["scope"]["enum"]
    assert "full" in props["scope"]["enum"]


def test_optional_scalar_nullable_mcp():
    from plugin.writer.content import GetDocumentContent

    props = to_mcp_schema(GetDocumentContent())["inputSchema"]["properties"]
    assert props["max_chars"]["type"] == ["integer", "null"]
    assert props["document_url"]["type"] == ["string", "null"]


def test_required_scalar_not_nullable():
    props = to_openai_schema(DummyTool())["function"]["parameters"]["properties"]
    assert props["arg1"]["type"] == "string"


def test_to_mcp_schema_delegate_writer_includes_specialized_delegation_hint():
    from plugin.writer.specialized_base import DelegateToSpecializedWriter

    tool = DelegateToSpecializedWriter()
    openai_schema = to_openai_schema(tool)
    mcp_schema = to_mcp_schema(tool)

    assert "specialized Writer task" not in openai_schema["function"]["description"]
    assert "specialized Writer task" in mcp_schema["description"]
    assert "\n" not in mcp_schema["description"]
    domain_desc = mcp_schema["inputSchema"]["properties"]["domain"]["description"]
    assert "domain one of:" in domain_desc
    assert "bookmarks:" in domain_desc
    assert "\n" not in domain_desc
    assert mcp_schema["inputSchema"]["properties"]["domain"]["description"] != "The specialized domain to activate."
    domain_enum = mcp_schema["inputSchema"]["properties"]["domain"]["enum"]
    assert "brainstorming" not in domain_enum
    assert "writing_plan" not in domain_enum
    assert "brainstorming:" not in domain_desc
    assert "writing_plan:" not in domain_desc


def test_to_mcp_schema_delegate_calc_domain_list_omits_analysis_and_python():
    from plugin.calc.specialized import DelegateToSpecializedCalc

    mcp_schema = to_mcp_schema(DelegateToSpecializedCalc())
    domain_desc = mcp_schema["inputSchema"]["properties"]["domain"]["description"]
    domain_enum = mcp_schema["inputSchema"]["properties"]["domain"]["enum"]
    assert "specialized Calc task" in mcp_schema["description"]
    assert "python" not in domain_enum
    assert "analysis" not in domain_enum
    assert "solvers" not in domain_enum
    assert "analysis:" not in domain_desc
    assert "python:" not in domain_desc


def test_update_style_schema_emits_no_additional_properties_keyword():
    """xAI/OpenRouter reject nested additionalProperties; StyleUpdate uses exhaustive properties only."""
    from plugin.writer.styles import StyleUpdate

    schema = to_openai_schema(StyleUpdate())
    wire = json.dumps(schema["function"]["parameters"])
    assert "additionalProperties" not in wire
    assert "property_updates" in schema["function"]["parameters"]["properties"]


def test_write_formula_range_mcp_widens_values_to_string_or_array():
    """#374 Bug 2: MCP hosts reject native arrays when schema is string-only."""
    from plugin.calc.cells import WriteCellRange

    tool = WriteCellRange()
    openai = to_openai_schema(tool)["function"]["parameters"]["properties"]["values"]
    mcp = to_mcp_schema(tool)["inputSchema"]["properties"]["values"]
    assert openai["type"] == "string"
    assert mcp["type"] == ["string", "array"]
    assert mcp["items"]["type"] == ["string", "number"]


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


def test_list_conditional_formats_range_is_array_mcp_widens():
    """list_conditional_formats uses the same array range schema as other Calc tools."""
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
        assert flags["items"]["enum"] == [
            "filter_criteria",
            "print_area",
            "column_header",
            "row_header",
        ]


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


def test_set_style_schema_omits_number_format_but_scripting_may_pass_it():
    """#374 P3: number_format must not appear on LLM/MCP schemas."""
    from plugin.calc.cells import SetCellStyle
    from plugin.framework.tool import ToolRegistry

    tool = SetCellStyle()
    assert "number_format" not in tool.parameters["properties"]
    assert "number_format" not in to_openai_schema(tool)["function"]["parameters"]["properties"]
    assert "number_format" not in to_mcp_schema(tool)["inputSchema"]["properties"]

    registry = ToolRegistry(MagicMock())
    registry.register(tool)
    ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.caller = "script"
    ctx.read_only_target = False
    ctx.uno_services_supported = frozenset({"com.sun.star.sheet.SpreadsheetDocument"})
    with patch.object(tool, "execute_safe", return_value={"status": "ok"}) as exe, patch(
        "plugin.framework.tool.execute_on_main_thread", side_effect=lambda fn: fn()
    ):
        # Unknown LLM keys still stripped; scripting_only number_format is preserved.
        registry.execute(
            "set_style",
            ctx,
            range=["A1"],
            bold=True,
            number_format="0.00",
            bogus_llm_key=1,
        )
        exe.assert_called_once()
        kwargs = exe.call_args.kwargs
        assert kwargs.get("number_format") == "0.00"
        assert "bogus_llm_key" not in kwargs


@pytest.mark.parametrize("caller", ["chat", "chatbot", "mcp"])
def test_set_style_strips_number_format_for_non_script_callers(caller):
    """§11.1: chat/MCP callers must not be able to apply scripting_only_parameters
    like number_format even if the model invents the parameter from training memory."""
    from plugin.calc.cells import SetCellStyle
    from plugin.framework.tool import ToolRegistry

    tool = SetCellStyle()
    registry = ToolRegistry(MagicMock())
    registry.register(tool)
    ctx = MagicMock()
    ctx.doc_type = "calc"
    ctx.caller = caller
    ctx.read_only_target = False
    ctx.uno_services_supported = frozenset({"com.sun.star.sheet.SpreadsheetDocument"})
    with patch.object(tool, "execute_safe", return_value={"status": "ok"}) as exe, patch(
        "plugin.framework.tool.execute_on_main_thread", side_effect=lambda fn: fn()
    ):
        registry.execute(
            "set_style",
            ctx,
            range=["A1"],
            bold=True,
            number_format="0.00",
        )
        exe.assert_called_once()
        kwargs = exe.call_args.kwargs
        # number_format must be stripped — it is not in the schema and caller is not "script".
        assert "number_format" not in kwargs
        # Regular schema params still pass through.
        assert kwargs.get("bold") is True

