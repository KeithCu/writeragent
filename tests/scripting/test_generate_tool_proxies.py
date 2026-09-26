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

def test_method_docstring_includes_full_description_and_schema_args():
    """shape_upsert-style schema text must survive into the proxy docstring."""
    tool = MockTool(
        "shape_upsert",
        (
            "Create or edit a shape on a page. When filling a paper-form blank, edit by shape Name "
            "from get_draw_tree — draw-page index shifts when other shapes sit between fields."
        ),
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "edit"],
                    "description": "Action to perform: 'create' a new shape, or 'edit' an existing one.",
                },
                "index": {
                    "type": "integer",
                    "description": "0-based index of the shape on the page (edit: pass index or name)",
                },
            },
            "required": ["action"],
        },
        specialized_domain="shapes",
    )
    code = generate_module([_as_tool(tool)])
    compile(code, "<generated>", "exec")
    assert "When filling a paper-form blank" in code
    assert "Action to perform: 'create' a new shape, or 'edit' an existing one." in code
    assert "One of: create, edit." in code
    assert "0-based index of the shape on the page (edit: pass index or name)." in code
    assert "action (required):" in code
    assert "index (optional):" in code
    assert "-> dict[str, Any]:" in code
    namespace: dict[str, object] = {}
    exec(code, namespace)
    doc = namespace["shape"].upsert.__doc__  # type: ignore[attr-defined]
    assert doc is not None
    assert "get_draw_tree" in doc
    assert "Action to perform: 'create' a new shape" in doc


def test_method_docstring_escapes_backslashes_and_triple_quotes():
    tool = MockTool(
        "weird_tool",
        'Keep C:\\temp and """quotes""" intact.\n\nSecond paragraph.',
        {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": 'Say """hi""" and \\n'},
            },
            "required": ["note"],
        },
    )
    code = generate_module([_as_tool(tool)])
    compile(code, "<generated>", "exec")
    namespace: dict[str, object] = {}
    exec(code, namespace)
    doc = namespace["core"].weird_tool.__doc__  # type: ignore[attr-defined]
    assert doc is not None
    assert 'C:\\temp' in doc
    assert '"""quotes"""' in doc
    assert "Second paragraph." in doc
    assert 'Say """hi""" and \\n.' in doc


def test_group_tools_drops_sidebar_chat_domains():
    from scripts.generate_tool_proxies import API_EXCLUDED_DOMAINS

    tools = [
        _as_tool(MockTool(f"{domain}_tool", "desc", {}, specialized_domain=domain))
        for domain in API_EXCLUDED_DOMAINS
    ]
    tools.append(_as_tool(MockTool("footnotes_insert", "Insert footnote.", {}, specialized_domain="footnotes")))
    tools.append(_as_tool(MockTool("shape_upsert", "Create or edit a shape.", {}, specialized_domain="shapes")))
    groups = group_tools(tools)
    for domain in API_EXCLUDED_DOMAINS:
        assert domain not in groups
        assert domain.replace("-", "_") not in groups
    assert "footnote" in groups
    assert "shape" in groups

    code = generate_module(tools)
    compile(code, "<generated>", "exec")
    for domain in API_EXCLUDED_DOMAINS:
        assert domain not in code
    assert "'footnote':" in code
    assert "'shape':" in code
    assert "class _FootnoteProxy:" in code
    assert "class _ShapeProxy:" in code


def test_shipped_writeragent_api_rich_docs_and_omitted_chat_domains():
    """Committed proxy: shape_upsert Args text, chat-mode domains gone, shapes/footnotes kept.

    Delegate gateway descriptions still name ``document_research`` as a chat domain
    argument, so that word can remain in another tool's docstring. The domain itself
    must not be a DOMAIN_TOOLS key or a proxy class.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    api_path = os.path.join(repo_root, "plugin", "scripting", "writeragent_api.py")
    with open(api_path, encoding="utf-8") as handle:
        source = handle.read()
    compile(source, api_path, "exec")

    assert "Action to perform: 'create' a new shape, or 'edit' an existing one." in source
    assert "0-based index of the shape on the page (edit: pass index or name)." in source
    assert "When filling a paper-form blank" in source
    assert "def upsert(self, action: str" in source
    assert "-> dict[str, Any]:" in source

    absent_markers = (
        "'writing_plan':",
        "'deep_research':",
        "'brainstorming':",
        "'document_research':",
        "'ppt-master':",
        "'ppt_master':",
        "class _WritingPlanProxy:",
        "class _DeepResearchProxy:",
        "class _BrainstormingProxy:",
        "class _DocumentResearchProxy:",
        "class _PptMasterProxy:",
        "writing_plan = ",
        "deep_research = ",
        "brainstorming = ",
        "document_research = ",
        "ppt_master = ",
    )
    for marker in absent_markers:
        assert marker not in source
    for domain in ("writing_plan", "deep_research", "brainstorming", "ppt-master", "ppt_master"):
        assert domain not in source
    assert "'shape':" in source
    assert "'footnote':" in source


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
    """Bare dict/list would trip reportMissingTypeArgument; generated proxies stay parameterized."""
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
