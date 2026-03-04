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
