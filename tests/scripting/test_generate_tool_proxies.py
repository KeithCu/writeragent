import sys
import os
import pytest
from unittest.mock import MagicMock

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
    pos, kw = schema_to_signature(tool)
    assert pos == ["req: str"]
    assert kw == ["opt: int = 10", "opt2: bool = True"]

def test_schema_to_signature_empty_schema():
    tool = MockTool("test_tool", "desc", {})
    pos, kw = schema_to_signature(tool)
    assert pos == []
    assert kw == []

def test_group_tools_by_domain():
    tools = [
        MockTool("footnotes_insert", "Insert footnote.", {}, specialized_domain="footnotes"),
        MockTool("footnotes_list", "List footnotes.", {}, specialized_domain="footnotes"),
        MockTool("bookmark_add", "Add bookmark.", {}, specialized_domain="bookmarks"),
        MockTool("get_doc_tree", "Get tree.", {}, specialized_domain=None, tier="core"),
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
        MockTool("footnotes_insert", "Insert footnote.", {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, specialized_domain="footnotes"),
    ]
    code = generate_module(tools)
    # Should compile without error
    compile(code, "<generated>", "exec")
    
    assert "class _FootnoteProxy:" in code
    assert "def insert(self, text: str) -> dict:" in code
    assert 'return _rpc_call("footnotes_insert", text=text)' in code
    assert "footnote = _FootnoteProxy()" in code
    assert "DOMAIN_TOOLS =" in code

def test_method_names_strip_prefix_plural():
    tools = [
        MockTool("footnotes_insert", "desc", {}, specialized_domain="footnotes"),
        MockTool("footnote_insert", "desc", {}, specialized_domain="footnotes"),
    ]
    groups = group_tools(tools)
    # Both should become "insert" if prefix matches
    method_names = [name for name, _ in groups["footnote"]]
    assert "insert" in method_names

def test_rpc_call_logic_in_generated_code():
    tools = [MockTool("t", "d", {})]
    code = generate_module(tools)
    assert "_rpc_call" in code
    assert "json.dumps(request)" in code
    assert "sys.stdin.readline()" in code
