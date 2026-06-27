import json

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


def test_to_mcp_schema_delegate_calc_domain_list_omits_python():
    from plugin.calc.specialized import DelegateToSpecializedCalc

    mcp_schema = to_mcp_schema(DelegateToSpecializedCalc())
    domain_desc = mcp_schema["inputSchema"]["properties"]["domain"]["description"]
    domain_enum = mcp_schema["inputSchema"]["properties"]["domain"]["enum"]
    assert "specialized Calc task" in mcp_schema["description"]
    assert "python" not in domain_enum
    assert "analysis" in domain_enum
    assert "solvers" not in domain_enum
    assert "python:" not in domain_desc


def test_update_style_schema_emits_no_additional_properties_keyword():
    """xAI/OpenRouter reject nested additionalProperties; UpdateStyle uses exhaustive properties only."""
    from plugin.writer.styles import UpdateStyle

    schema = to_openai_schema(UpdateStyle())
    wire = json.dumps(schema["function"]["parameters"])
    assert "additionalProperties" not in wire
    assert "property_updates" in schema["function"]["parameters"]["properties"]

