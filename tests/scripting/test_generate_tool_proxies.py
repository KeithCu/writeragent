import sys
import os
import pytest
from typing import cast

from plugin.framework.tool import ToolBase

# Add project root to sys.path
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

try:
    from scripts.generate_tool_proxies import schema_to_signature, group_tools, generate_module
except ImportError:
    pytest.skip("scripts module not available (e.g., in bundled release builds)", allow_module_level=True)

class MockTool:
    def __init__(self, name, description, parameters, specialized_domain=None, tier="specialized"):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.specialized_domain = specialized_domain
        self.tier = tier


def _as_tool(tool: MockTool) -> ToolBase:
    return cast("ToolBase", tool)

def test_schema_to_signature_positional_and_keyword():
    tool = MockTool(
        "test_tool",
        "Test tool description.",
        {
            "type": "object",
            "properties": {
                "req": {"type": "string"},
                "opt": {"type": "integer", "default": 10},
                "opt2": {"type": "boolean"}
            },
            "required": ["req"]
        }
    )
    pos, kw = schema_to_signature(_as_tool(tool))
    assert pos == ["req: str"]
    assert kw == ["opt: int = 10", "opt2: bool | None = None"]


def test_schema_to_signature_optional_bool_without_default_is_none():
    """apply_document_content dry_run has no schema default; True would be a silent no-op."""
    tool = MockTool(
        "apply_document_content",
        "Insert or replace content.",
        {
            "type": "object",
            "properties": {
                "content": {"type": "array"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["content"],
        },
    )
    pos, kw = schema_to_signature(_as_tool(tool))
    assert pos == ["content: list[Any]"]
    assert kw == ["dry_run: bool | None = None"]

def test_schema_to_signature_empty_schema():
    tool = MockTool("test_tool", "desc", {})
    pos, kw = schema_to_signature(_as_tool(tool))
    assert pos == []
    assert kw == []

def test_group_tools_by_domain():
    tools = [
        _as_tool(MockTool("footnotes_insert", "Insert footnote.", {}, specialized_domain="footnotes")),
        _as_tool(MockTool("footnotes_list", "List footnotes.", {}, specialized_domain="footnotes")),
        _as_tool(MockTool("bookmark_add", "Add bookmark.", {}, specialized_domain="bookmarks")),
        _as_tool(MockTool("get_doc_tree", "Get tree.", {}, specialized_domain=None, tier="core")),
    ]
    groups = group_tools(tools)
    
    # Check grouping and prefix stripping
    assert "footnote" in groups
    assert "bookmark" in groups
    assert "core" in groups
    
    # Method names
    assert any(name == "insert" for name, _ in groups["footnote"])
    assert any(name == "list" for name, _ in groups["footnote"])
    assert any(name == "add" for name, _ in groups["bookmark"])
    assert any(name == "get_doc_tree" for name, _ in groups["core"])

def test_generate_module_output_is_valid_python():
    tools = [
        _as_tool(MockTool("footnotes_insert", "Insert footnote.", {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, specialized_domain="footnotes")),
    ]
    code = generate_module(tools)
    # Should compile without error
    compile(code, "<generated>", "exec")
    
    assert "class _FootnoteProxy:" in code
    assert "def insert(self, text: str) -> dict[str, Any]:" in code
    assert 'return _rpc_call("footnotes_insert", text=text)' in code
    assert "footnote = _FootnoteProxy()" in code
    assert "DOMAIN_TOOLS =" in code


def test_generate_module_escapes_python_keyword_method_names():
    tools = [
        _as_tool(
            MockTool(
                "style_import",
                "Import styles from a file.",
                {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
                specialized_domain="styles",
            )
        ),
    ]
    code = generate_module(tools)
    compile(code, "<generated>", "exec")
    assert "def import_(self, file_path: str) -> dict[str, Any]:" in code
    assert 'return _rpc_call("style_import", file_path=file_path)' in code
    assert "def import(self" not in code

def test_method_names_strip_prefix_plural():
    tools = [
        _as_tool(MockTool("footnotes_insert", "desc", {}, specialized_domain="footnotes")),
        _as_tool(MockTool("footnote_insert", "desc", {}, specialized_domain="footnotes")),
    ]
    groups = group_tools(tools)
    # Both should become "insert" if prefix matches
    method_names = [name for name, _ in groups["footnote"]]
    assert "insert" in method_names

def test_first_sentence_strips_trailing_period():
    from scripts.generate_tool_proxies import _first_sentence

    assert _first_sentence("Deletes a shape by index.") == "Deletes a shape by index."
    assert _first_sentence("One. Two.") == "One."
    assert _first_sentence("") == ""


def test_range_schema_becomes_range_name_python_param():
    tool = MockTool(
        "read_cell_range",
        "Read cells.",
        {
            "type": "object",
            "properties": {"range": {"type": "array", "items": {"type": "string"}}},
            "required": ["range"],
        },
    )
    pos, kw = schema_to_signature(_as_tool(tool))
    assert pos == ["range_name: list[str]"]
    assert kw == []
    code = generate_module([_as_tool(tool)])
    compile(code, "<generated>", "exec")
    assert "def read_cell_range(self, range_name: list[str]) -> dict[str, Any]:" in code
    assert 'return _rpc_call("read_cell_range", range=range_name)' in code


def test_schema_to_signature_parameterizes_object_and_array():
    """Bare dict/list would trip reportMissingTypeArgument; keep that rule off."""
    tool = MockTool(
        "test_tool",
        "desc",
        {
            "type": "object",
            "properties": {
                "props": {"type": "object"},
                "names": {"type": "array", "items": {"type": "string"}},
                "rows": {"type": "array"},
            },
            "required": ["props", "names", "rows"],
        },
    )
    pos, kw = schema_to_signature(_as_tool(tool))
    assert pos == ["props: dict[str, Any]", "names: list[str]", "rows: list[Any]"]
    assert kw == []


def test_rpc_call_logic_in_generated_code():
    tools = [_as_tool(MockTool("t", "d", {}))]
    code = generate_module(tools)
    assert "def _rpc_call(tool_name: str, **kwargs: Any) -> dict[str, Any]:" in code
    assert "kwargs = {k: v for k, v in kwargs.items() if v is not None}" in code
    assert "from plugin.scripting.host_rpc import execute_tool" in code
    assert "write_pickle_frame(sys.stdout.buffer, request)" in code
    assert "max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES" in code
    assert "read_pickle_frame(" in code


def test_indexes_domain_becomes_index():
    tools = [
        _as_tool(MockTool("indexes_create", "Create index.", {}, specialized_domain="indexes")),
    ]
    groups = group_tools(tools)
    assert "index" in groups
    assert "indexe" not in groups
    code = generate_module(tools)
    compile(code, "<generated>", "exec")
    assert "class _IndexProxy:" in code
    assert "index = _IndexProxy()" in code
