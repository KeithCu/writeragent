"""Tests for plugin.framework.schema_convert."""

from plugin.framework.tool_base import ToolBase
from plugin.framework.schema_convert import to_openai_schema, to_mcp_schema


class SampleTool(ToolBase):
    name = "sample_tool"
    description = "A sample tool"
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Input text"},
        },
        "required": ["text"],
    }

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


class MinimalTool(ToolBase):
    name = "minimal"
    description = ""
    parameters = None

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


from plugin.framework.schema_convert import _normalize_schema_for_strict_providers

class TestNormalizeSchema:
    def test_normalize_empty_or_none(self):
        assert _normalize_schema_for_strict_providers(None) is None
        assert _normalize_schema_for_strict_providers("string") == "string"
        assert _normalize_schema_for_strict_providers({}) == {}

    def test_normalize_union_type(self):
        schema = {"type": ["string", "null"]}
        normalized = _normalize_schema_for_strict_providers(schema)
        assert normalized["type"] == "string"

        schema = {"type": ["string", "array"]}
        normalized = _normalize_schema_for_strict_providers(schema)
        assert normalized["type"] == "array"

        schema = {"type": []}
        normalized = _normalize_schema_for_strict_providers(schema)
        assert normalized["type"] == "string"

    def test_normalize_removes_items_if_not_array(self):
        schema = {"type": "string", "items": {"type": "string"}}
        normalized = _normalize_schema_for_strict_providers(schema)
        assert "items" not in normalized

    def test_normalize_removes_empty_required(self):
        schema = {"type": "object", "required": []}
        normalized = _normalize_schema_for_strict_providers(schema)
        assert "required" not in normalized

    def test_normalize_recursive_properties(self):
        schema = {
            "type": "object",
            "properties": {
                "prop1": {"type": ["string", "null"]},
                "prop2": {"type": "object", "properties": {"nested": {"type": ["number", "null"]}}}
            }
        }
        normalized = _normalize_schema_for_strict_providers(schema)
        assert normalized["properties"]["prop1"]["type"] == "string"
        assert normalized["properties"]["prop2"]["properties"]["nested"]["type"] == "number"

    def test_normalize_recursive_items(self):
        schema = {
            "type": "array",
            "items": {"type": ["string", "null"]}
        }
        normalized = _normalize_schema_for_strict_providers(schema)
        assert normalized["items"]["type"] == "string"

        schema_list_items = {
            "type": "array",
            "items": [{"type": ["string", "null"]}]
        }
        normalized_list_items = _normalize_schema_for_strict_providers(schema_list_items)
        assert normalized_list_items["items"]["type"] == "string"

class TestToOpenaiSchema:
    def test_full_schema(self):
        schema = to_openai_schema(SampleTool())
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == "sample_tool"
        assert fn["description"] == "A sample tool"
        assert fn["parameters"]["type"] == "object"
        assert "text" in fn["parameters"]["properties"]
        assert fn["parameters"]["required"] == ["text"]

    def test_minimal_schema(self):
        schema = to_openai_schema(MinimalTool())
        fn = schema["function"]
        assert fn["name"] == "minimal"
        assert fn["parameters"]["type"] == "object"

    def test_does_not_mutate_original(self):
        tool = SampleTool()
        original_params = tool.parameters.copy()
        to_openai_schema(tool)
        assert tool.parameters == original_params


class TestToMcpSchema:
    def test_full_schema(self):
        schema = to_mcp_schema(SampleTool())
        assert schema["name"] == "sample_tool"
        assert schema["description"] == "A sample tool"
        assert schema["inputSchema"]["type"] == "object"
        assert "text" in schema["inputSchema"]["properties"]

    def test_minimal_schema(self):
        schema = to_mcp_schema(MinimalTool())
        assert schema["name"] == "minimal"
        assert schema["inputSchema"]["type"] == "object"
