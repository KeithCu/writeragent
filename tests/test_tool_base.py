"""Tests for plugin.framework.tool_base."""

import pytest

from plugin.framework.tool_base import ToolBase


class ReadTool(ToolBase):
    name = "get_something"
    description = "Reads something"
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


class WriteTool(ToolBase):
    name = "apply_content"
    description = "Writes content"
    parameters = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    }

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


class ExplicitMutationTool(ToolBase):
    name = "get_but_actually_writes"
    is_mutation = True
    parameters = {}

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


class NoParamsTool(ToolBase):
    name = "do_thing"
    parameters = None

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


class TestDetectsMutation:
    def test_read_prefix_not_mutation(self):
        for prefix in ("get_", "read_", "list_", "find_", "search_", "count_"):
            tool = ReadTool()
            tool.name = f"{prefix}something"
            assert tool.detects_mutation() is False

    def test_write_prefix_is_mutation(self):
        t = WriteTool()
        assert t.detects_mutation() is True

    def test_explicit_override(self):
        t = ExplicitMutationTool()
        assert t.detects_mutation() is True

    def test_no_name_defaults_to_mutation(self):
        t = WriteTool()
        t.name = None
        assert t.detects_mutation() is True


class TestValidate:
    def test_valid_params(self):
        t = ReadTool()
        ok, err = t.validate(id="abc")
        assert ok is True
        assert err is None

    def test_missing_required(self):
        t = ReadTool()
        ok, err = t.validate()
        assert ok is False
        assert "Missing required" in err

    def test_unknown_param(self):
        t = ReadTool()
        ok, err = t.validate(id="abc", bogus="x")
        assert ok is False
        assert "Unknown parameter" in err

    def test_no_schema_accepts_anything(self):
        t = NoParamsTool()
        ok, err = t.validate(anything="goes")
        assert ok is True
