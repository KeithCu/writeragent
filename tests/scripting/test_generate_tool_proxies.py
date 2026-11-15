import sys
import os
import pytest
from typing import Any, cast

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
    def __init__(self, name, description, parameters, specialized_domain=None, tier="specialized", doc_types=None):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.specialized_domain = specialized_domain
        self.tier = tier
        self.doc_types = doc_types


def _as_tool(tool: MockTool) -> ToolBase:
    return cast("ToolBase", tool)

def test_schema_to_signature_positional_and_keyword():
    tool = MockTool(
        "calc_test",
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
    """apply_content dry_run has no schema default; True would be a silent no-op."""
    tool = MockTool(
        "apply_content",
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
    tool = MockTool("calc_test", "desc", {})
    pos, kw = schema_to_signature(_as_tool(tool))
    assert pos == []
    assert kw == []

def test_group_tools_by_domain():
    tools = [
        _as_tool(MockTool("footnote_insert", "Insert footnote.", {}, specialized_domain="footnotes")),
        _as_tool(MockTool("footnote_list", "List footnotes.", {}, specialized_domain="footnotes")),
        _as_tool(MockTool("bookmark_add", "Add bookmark.", {}, specialized_domain="bookmarks")),
        _as_tool(MockTool("get_tree", "Get tree.", {}, specialized_domain=None, tier="core", doc_types=["writer"])),
    ]
    groups = group_tools(tools)

    assert "footnote" in groups
    assert "bookmark" in groups
    assert "writer" in groups

    # Method names
    assert any(name == "insert" for name, _ in groups["footnote"])
    assert any(name == "list" for name, _ in groups["footnote"])
    assert any(name == "add" for name, _ in groups["bookmark"])
    assert any(name == "get_tree" for name, _ in groups["writer"])

def test_generate_module_output_is_valid_python():
    tools = [
        _as_tool(MockTool("footnote_insert", "Insert footnote.", {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, specialized_domain="footnotes")),
    ]
    code = generate_module(tools)
    # Should compile without error
    compile(code, "<generated>", "exec")
    
    assert "class _FootnoteProxy:" in code
    assert "def insert(self, text: str) -> _Item:" in code
    assert 'return _result(call_host_tool("footnote_insert", text=text))' in code
    assert "footnote = _FootnoteProxy()" in code
    assert "DOMAIN_TOOLS =" in code


def test_generate_module_domain_with_slash_is_a_valid_identifier():
    """plugin/calc/base.py registers the domain "python/sql": DOMAIN_TOOLS keeps the slash key."""
    tools = [
        _as_tool(MockTool("sql_query_folder", "Run SQL.", {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}, specialized_domain="python/sql")),
    ]
    code = generate_module(tools)
    compile(code, "<generated>", "exec")
    assert "class _SqlProxy:" in code
    assert "sql = _SqlProxy()" in code
    assert "'python/sql': ['sql_query_folder']" in code
    ns: dict[str, Any] = {}
    exec(code, ns)
    assert ns["TOOL_METHODS"]["sql_query_folder"] == ("sql", "query_folder")


def test_generator_script_runs_without_libreoffice(tmp_path):
    """Outside LibreOffice bootstrap got a mock UNO context, init_config rejected its path and the
    script died; `make proxy-stubs` then left writeragent_api.py empty."""
    import subprocess

    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script = os.path.join(repo, "scripts", "generate_tool_proxies.py")
    result = subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=240, cwd=repo)
    assert result.returncode == 0, result.stderr[-2000:]
    compile(result.stdout, "<generated>", "exec")
    assert "DOMAIN_TOOLS =" in result.stdout
    api_path = os.path.join(repo, "plugin", "scripting", "writeragent_api.py")
    with open(api_path, encoding="utf-8") as handle:
        expected = handle.read()
    assert result.stdout == expected, (
        "plugin/scripting/writeragent_api.py is out of date; run `make proxy-stubs` to regenerate."
    )


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
    assert "def import_(self, file_path: str) -> _Item:" in code
    assert 'return _result(call_host_tool("style_import", file_path=file_path))' in code
    assert "def import(self" not in code
    ns: dict[str, Any] = {}
    exec(code, ns)
    assert ns["TOOL_METHODS"]["style_import"] == ("style", "import_")


def test_generate_module_same_tool_name_twice_does_not_raise():
    tool = _as_tool(
        MockTool(
            "footnote_insert",
            "Insert footnote.",
            {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            specialized_domain="footnotes",
        )
    )
    code = generate_module([tool, tool])
    compile(code, "<generated>", "exec")

def test_method_docstring_includes_full_description_and_schema_args():
    """shape_upsert-style schema text must survive into the proxy docstring."""
    tool = MockTool(
        "shape_upsert",
        (
            "Create or edit a shape on a page. When filling a paper-form blank, edit by shape Name "
            "from get_tree — draw-page index shifts when other shapes sit between fields."
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
    assert "-> _Item:" in code
    namespace: dict[str, Any] = {}
    exec(code, namespace)
    doc = namespace["shape"].upsert.__doc__
    assert doc is not None
    assert "get_tree" in doc
    assert "Action to perform: 'create' a new shape" in doc


def test_method_docstring_escapes_backslashes_and_triple_quotes():
    tool = MockTool(
        "weird",
        'Keep C:\\temp and """quotes""" intact.\n\nSecond paragraph.',
        {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": 'Say """hi""" and \\n'},
            },
            "required": ["note"],
        },
        doc_types=["calc"],
    )
    code = generate_module([_as_tool(tool)])
    compile(code, "<generated>", "exec")
    namespace: dict[str, Any] = {}
    exec(code, namespace)
    doc = namespace["calc"].weird.__doc__
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
    tools.append(_as_tool(MockTool("footnote_insert", "Insert footnote.", {}, specialized_domain="footnotes")))
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
    assert "'footnotes':" in code
    assert "'shapes':" in code
    assert "class _FootnoteProxy:" in code
    assert "class _ShapeProxy:" in code
    assert "web_research" not in API_EXCLUDED_DOMAINS


def test_group_tools_drops_llm_orchestration_tools():
    """Delegate/finish tools are chat orchestration, not venv script methods.

    A copied description may still mention the Calc delegate; that breadcrumb
    is not a method or a DOMAIN_TOOLS entry.
    """
    from scripts.generate_tool_proxies import API_EXCLUDED_TOOLS

    tools = [
        _as_tool(
            MockTool(
                name,
                "Orchestrate a specialized toolset.",
                {
                    "type": "object",
                    "properties": {"task": {"type": "string"}},
                    "required": ["task"],
                },
                specialized_domain=None,
                tier="core",
            )
        )
        for name in sorted(API_EXCLUDED_TOOLS)
    ]
    tools.append(
        _as_tool(
            MockTool(
                "table_apply_filter",
                "Hide rows. calc_delegate_specialized(domain='sheets').",
                {
                    "type": "object",
                    "properties": {"range": {"type": "array", "items": {"type": "string"}}},
                    "required": ["range"],
                },
                specialized_domain="sheets",
            )
        )
    )
    groups = group_tools(tools)
    emitted = [tool.name for entries in groups.values() for _short, tool in entries]
    for name in API_EXCLUDED_TOOLS:
        assert name not in emitted
    assert "table" in groups

    code = generate_module(tools)
    compile(code, "<generated>", "exec")
    for name in API_EXCLUDED_TOOLS:
        assert f"def {name}(" not in code
        assert f"'{name}'" not in code
        assert f'call_host_tool("{name}"' not in code
    assert "calc_delegate_specialized(domain='sheets')" in code
    assert "def apply_filter(" in code


@pytest.mark.parametrize(
    ("tool_name", "app", "expected"),
    [
        ("page_comment_add", None, ("page_comment", "add")),
        ("page_get_columns", None, ("page", "get_columns")),
        ("named_range_get_info", None, ("named_range", "get_info")),
        ("sheet_apply_filter", None, ("sheet", "apply_filter")),
        ("style_import", None, ("style", "import_")),
        ("get_sheet_summary", "calc", ("calc", "get_sheet_summary")),
        ("get_tree", "writer", ("writer", "get_tree")),
        ("shape_get_tree", None, ("shape", "get_tree")),
        ("get_deck_info", "draw", ("draw", "get_deck_info")),
    ],
)
def test_split_tool_name(tool_name, app, expected):
    from scripts.generate_tool_proxies import split_tool_name

    assert split_tool_name(tool_name, app=app) == expected


@pytest.mark.parametrize("bad_name", ["writer_get_tree", "calc_read_range", "draw_get_info"])
def test_split_tool_name_rejects_app_prefix(bad_name):
    from scripts.generate_tool_proxies import split_tool_name

    with pytest.raises(ValueError, match="app prefix"):
        split_tool_name(bad_name, app="writer")


def test_unknown_domain_raises():
    tools = [_as_tool(MockTool("fields_list", "List fields.", {}, specialized_domain="fields"))]
    with pytest.raises(ValueError, match=r"fields_list.*API_DOMAINS"):
        generate_module(tools)


@pytest.mark.parametrize("bad_domain", ["help", "doc", "scripts", "vision", "range", "import"])
def test_check_api_domains_rejects(bad_domain):
    from scripts.generate_tool_proxies import check_api_domains

    with pytest.raises(ValueError):
        check_api_domains(frozenset({bad_domain}))


def test_rule_collision_raises():
    tools = [
        _as_tool(MockTool("style_import", "desc", {}, specialized_domain="styles")),
        _as_tool(MockTool("style_import_", "desc", {}, specialized_domain="styles")),
    ]
    with pytest.raises(
        ValueError,
        match=r"proxy method style\.import_ is produced by both",
    ):
        generate_module(tools)


def test_domain_tools_key_is_specialized_domain():
    tools = [
        _as_tool(MockTool("sort_range", "Sort.", {}, specialized_domain="ranges", doc_types=["calc"])),
        _as_tool(MockTool("named_range_add", "Add.", {}, specialized_domain="ranges", doc_types=["calc"])),
    ]
    code = generate_module(tools)
    compile(code, "<generated>", "exec")
    ns: dict[str, Any] = {}
    exec(code, ns)
    assert ns["DOMAIN_TOOLS"]["ranges"] == ["named_range_add", "sort_range"]
    assert ns["TOOL_METHODS"]["sort_range"] == ("calc", "sort_range")
    assert ns["TOOL_METHODS"]["named_range_add"] == ("named_range", "add")


def test_range_schema_becomes_range_name_python_param():
    tool = MockTool(
        "read_range",
        "Read cells.",
        {
            "type": "object",
            "properties": {
                "range": {"type": "array", "items": {"type": "string"}},
                "format": {"type": "string", "enum": ["rows", "cells"], "default": "rows"},
            },
            "required": ["range"],
        },
        doc_types=["calc"],
    )
    pos, kw = schema_to_signature(_as_tool(tool))
    assert pos == ["range_name: list[str]"]
    assert kw == ["format: str = 'rows'"]
    code = generate_module([_as_tool(tool)])
    compile(code, "<generated>", "exec")
    assert "def read_range(self, range_name: list[str], *, format: str = 'rows') -> _Item:" in code
    assert 'return _result(call_host_tool("read_range", range=range_name, format=format))' in code


def test_schema_to_signature_parameterizes_object_and_array():
    """Bare dict/list would trip reportMissingTypeArgument; generated proxies stay parameterized."""
    tool = MockTool(
        "calc_test",
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
    tools = [_as_tool(MockTool("t", "d", {}, doc_types=["calc"]))]
    code = generate_module(tools)
    assert "from plugin.scripting.host_tool_call import call_host_tool" in code
    assert "call_host_tool(" in code


def test_tool_methods_and_all_in_generated_code():
    tools = [
        _as_tool(MockTool("shape_upsert", "Upsert shape.", {}, specialized_domain="shapes")),
        _as_tool(MockTool("read_range", "Read cell range.", {}, specialized_domain="ranges", doc_types=["calc"])),
    ]
    code = generate_module(tools)
    ns: dict[str, Any] = {}
    exec(code, ns)
    assert "TOOL_METHODS" in ns
    assert ns["TOOL_METHODS"]["shape_upsert"] == ("shape", "upsert")
    assert "__all__ = [" in code
    assert "shape" in ns["__all__"]
    assert "range" not in ns["__all__"]


def test_import_generator_does_not_mutate_sys_modules_or_meta_path():
    import subprocess

    cmd = (
        "import sys\n"
        "meta_len = len(sys.meta_path)\n"
        "has_uno = 'uno' in sys.modules\n"
        "has_unohelper = 'unohelper' in sys.modules\n"
        "import scripts.generate_tool_proxies\n"
        "assert ('uno' in sys.modules) is False\n"
        "assert ('unohelper' in sys.modules) is False\n"
        "assert len(sys.meta_path) == meta_len\n"
        "assert not any(type(f).__name__ == 'MockFinder' for f in sys.meta_path)\n"
    )
    res = subprocess.run([sys.executable, "-c", cmd], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


class _DummyRegistry:
    def __init__(self, tools):
        self._tools = tools

    def get_tools(self, *args, **kwargs):
        return self._tools


def test_main_fails_closed_on_empty_registry(monkeypatch, tmp_path):
    from scripts import generate_tool_proxies

    monkeypatch.setattr("plugin.app.get_tools", lambda: _DummyRegistry([]))
    out_file = tmp_path / "writeragent_api.py"
    with pytest.raises(SystemExit) as exc_info:
        generate_tool_proxies.main(["--output", str(out_file)])
    assert exc_info.value.code != 0
    assert not out_file.exists()


def test_main_fails_closed_when_core_missing_document_list_open(monkeypatch, tmp_path):
    from scripts import generate_tool_proxies

    dummy_tool = _as_tool(MockTool("footnote_dummy", "desc", {}, specialized_domain="footnotes"))
    monkeypatch.setattr("plugin.app.get_tools", lambda: _DummyRegistry([dummy_tool]))
    out_file = tmp_path / "writeragent_api.py"
    with pytest.raises(SystemExit) as exc_info:
        generate_tool_proxies.main(["--output", str(out_file)])
    assert exc_info.value.code != 0
    assert not out_file.exists()


def test_main_check_flag(tmp_path):
    from scripts import generate_tool_proxies

    bad_file = tmp_path / "bad_api.py"
    bad_file.write_text("# corrupted\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        generate_tool_proxies.main(["--check", str(bad_file)])
    assert exc_info.value.code != 0


def test_method_docstring_trailing_quote_compiles():
    desc = 'Keep the trailing "quote"'
    tool = MockTool("trailing_quote", desc, {}, doc_types=["calc"])
    code = generate_module([_as_tool(tool)])
    compile(code, "<generated>", "exec")
    ns: dict[str, Any] = {}
    exec(code, ns)
    doc = ns["calc"].trailing_quote.__doc__
    assert doc is not None
    assert doc.strip() == desc


@pytest.mark.parametrize("bad_key", ["self", "foo-bar", "1x"])
def test_invalid_parameter_name_raises(bad_key):
    tool = MockTool(
        "bad",
        "desc",
        {
            "type": "object",
            "properties": {bad_key: {"type": "string"}},
            "required": [bad_key],
        },
        doc_types=["calc"],
    )
    with pytest.raises(ValueError, match=rf"Invalid parameter name {bad_key!r} in tool 'bad'"):
        generate_module([_as_tool(tool)])


def test_keyword_parameter_compiles_and_uses_splat():
    tool = MockTool(
        "keyword",
        "desc",
        {
            "type": "object",
            "properties": {
                "class": {"type": "string"},
                "reg": {"type": "integer"},
            },
            "required": ["class"],
        },
        doc_types=["calc"],
    )
    code = generate_module([_as_tool(tool)])
    compile(code, "<generated>", "exec")
    assert "def keyword(self, class_: str, *, reg: int | None = None) -> _Item:" in code
    assert 'return _result(call_host_tool("keyword", **{"class": class_}, reg=reg))' in code


def test_default_none_emits_optional_annotation():
    tool = MockTool(
        "test",
        "desc",
        {
            "type": "object",
            "properties": {
                "val": {"type": "string", "default": None},
            },
        },
        doc_types=["calc"],
    )
    pos, kw = schema_to_signature(_as_tool(tool))
    assert pos == []
    assert kw == ["val: str | None = None"]
    code = generate_module([_as_tool(tool)])
    compile(code, "<generated>", "exec")
    assert "def test(self, *, val: str | None = None) -> _Item:" in code


def test_union_array_string_type_emits_union_annotation():
    tool = MockTool(
        "apply_content",
        "desc",
        {
            "type": "object",
            "properties": {
                "content": {"type": ["array", "string"], "items": {"type": "string"}},
            },
            "required": ["content"],
        },
        doc_types=["writer"],
    )
    pos, kw = schema_to_signature(_as_tool(tool))
    assert pos == ["content: list[str] | str"]
    code = generate_module([_as_tool(tool)])
    compile(code, "<generated>", "exec")
    assert "def apply_content(self, content: list[str] | str) -> _Item:" in code


def test_proxy_qualifies_list_annotation_when_list_method_present():
    chart_upsert = MockTool(
        "chart_upsert",
        "Upsert chart.",
        {
            "type": "object",
            "properties": {
                "headers": {"type": "array", "items": {"type": "string"}},
            },
        },
        specialized_domain="charts",
    )
    chart_list = MockTool(
        "chart_list",
        "List charts.",
        {"type": "object", "properties": {}},
        specialized_domain="charts",
    )
    no_list = MockTool(
        "footnote_add",
        "Add footnote.",
        {
            "type": "object",
            "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
            "required": ["tags"],
        },
        specialized_domain="footnotes",
    )
    code = generate_module([_as_tool(chart_upsert), _as_tool(chart_list), _as_tool(no_list)])
    compile(code, "<generated>", "exec")
    assert "headers: builtins.list[str]" in code
    assert "def add(self, tags: list[str]" in code
    assert "tags: builtins.list[str]" not in code


def test_atomic_write_skips_rewrite_when_unchanged(tmp_path):
    from scripts.generate_tool_proxies import _atomic_write

    target = tmp_path / "writeragent_api.py"
    _atomic_write(str(target), "# same content\n")
    os.utime(target, (1_000_000_000, 1_000_000_000))
    mtime_before = target.stat().st_mtime_ns

    _atomic_write(str(target), "# same content\n")
    assert target.stat().st_mtime_ns == mtime_before

    _atomic_write(str(target), "# updated content\n")
    assert target.read_text(encoding="utf-8") == "# updated content\n"
    assert target.stat().st_mtime_ns != mtime_before


def test_duplicate_tool_name_fails_generation():
    tools = [
        _as_tool(MockTool("get_tree", "Writer tree.", {}, doc_types=["writer"])),
        _as_tool(MockTool("get_tree", "Draw tree.", {}, doc_types=["draw"])),
    ]
    with pytest.raises(ValueError, match="duplicate tool name 'get_tree'"):
        generate_module(tools)


def test_check_duplicate_registrations_rejects_clash_allows_shared():
    from plugin.framework.tool import ToolBase, ToolRegistry
    from scripts.generate_tool_proxies import check_duplicate_registrations

    class _WriterTree(ToolBase):
        name = "get_tree"
        description = "Writer tree."
        parameters = {"type": "object", "properties": {}}
        uno_services = ["com.sun.star.text.TextDocument"]

        def execute(self, ctx, **kwargs):
            return {"status": "ok"}

    class _DrawTree(ToolBase):
        name = "get_tree"
        description = "Draw tree."
        parameters = {"type": "object", "properties": {}}
        uno_services = ["com.sun.star.drawing.DrawingDocument"]

        def execute(self, ctx, **kwargs):
            return {"status": "ok"}

    reg = ToolRegistry(services=None)
    reg.register(_WriterTree())
    reg.register(_DrawTree())
    with pytest.raises(ValueError, match="duplicate tool name 'get_tree'"):
        check_duplicate_registrations(reg)

    # Deliberate shared classes (e.g. shape_upsert with the same class name) are allowed.
    def _make_shared(mod_name: str, svc: str) -> ToolBase:
        cls = type("UpsertShape", (ToolBase,), {
            "name": "shape_upsert",
            "description": "Upsert shape.",
            "parameters": {"type": "object", "properties": {}},
            "uno_services": [svc],
            "execute": lambda self, ctx, **kw: {"status": "ok"},
        })
        cls.__module__ = mod_name
        return cls()

    shared_reg = ToolRegistry(services=None)
    shared_reg.register(_make_shared("plugin.calc.shapes", "com.sun.star.sheet.SpreadsheetDocument"))
    shared_reg.register(_make_shared("plugin.draw.shapes", "com.sun.star.drawing.DrawingDocument"))
    check_duplicate_registrations(shared_reg)


def _exec_generated_proxy(tools: list[ToolBase], handler):
    code = generate_module(tools)
    compile(code, "<generated>", "exec")
    ns: dict[str, object] = {}
    exec(code, ns)
    calls: list[tuple[str, dict[str, object]]] = []

    def _fake(tool: str, **kwargs: object):
        calls.append((tool, kwargs))
        return handler(tool, kwargs)

    ns["call_host_tool"] = _fake
    return ns, calls


def _bookmark_tools() -> list[ToolBase]:
    return [
        _as_tool(MockTool("bookmark_list", "List bookmarks.", {"type": "object", "properties": {}}, specialized_domain="bookmarks")),
        _as_tool(
            MockTool(
                "bookmark_get",
                "Get bookmark.",
                {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                specialized_domain="bookmarks",
            )
        ),
        _as_tool(
            MockTool(
                "bookmark_delete",
                "Delete bookmark.",
                {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                specialized_domain="bookmarks",
            )
        ),
        _as_tool(
            MockTool(
                "bookmark_create",
                "Create bookmark.",
                {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                specialized_domain="bookmarks",
            )
        ),
    ]


def test_collection_namespace_is_a_container():
    def _handler(tool: str, kwargs: dict[str, object]):
        if tool == "bookmark_list":
            return {"status": "ok", "bookmarks": [{"name": "A", "text": "short"}, {"name": "B", "text": "b"}], "count": 2}
        if tool == "bookmark_get":
            if kwargs["name"] == "A":
                return {"status": "ok", "bookmark": {"name": "A", "text": "full"}}
            return {"status": "error", "code": "TOOL_EXECUTION_ERROR", "message": "not found"}
        if tool == "bookmark_delete":
            if kwargs["name"] == "A":
                return {"status": "ok", "deleted": "A"}
            return {"status": "error", "code": "TOOL_EXECUTION_ERROR", "message": "not found"}
        if tool == "bookmark_create":
            return {"status": "ok", "name": kwargs["name"]}
        raise AssertionError(tool)

    ns, calls = _exec_generated_proxy(_bookmark_tools(), _handler)
    bm = ns["bookmark"]

    assert [item.name for item in bm] == ["A", "B"]
    assert len(bm) == 2
    assert "A" in bm
    assert "Z" not in bm
    assert bm["A"].text == "full"
    assert bm["A"]["name"] == "A"

    with pytest.raises(KeyError):
        _ = bm["Z"]
    with pytest.raises(KeyError):
        del bm["Z"]

    del bm["A"]
    assert ("bookmark_delete", {"name": "A"}) in calls
    assert not hasattr(bm, "list")
    assert not hasattr(bm, "get")
    assert not hasattr(bm, "delete")

    assert bm.create("C") == {"name": "C"}
    assert ns["TOOL_METHODS"] == {
        "bookmark_list": ("bookmark", "__iter__"),
        "bookmark_get": ("bookmark", "__getitem__"),
        "bookmark_delete": ("bookmark", "__delitem__"),
        "bookmark_create": ("bookmark", "create"),
    }


def test_collection_error_for_present_name_raises_runtime_error_with_code():
    list_fails = False

    def _handler(tool: str, kwargs: dict[str, object]):
        if tool == "bookmark_list":
            if list_fails:
                return {"status": "error", "code": "LIST_FAIL", "message": "list broke"}
            return {"status": "ok", "bookmarks": [{"name": "A"}], "count": 1}
        if tool in ("bookmark_get", "bookmark_delete"):
            return {"status": "error", "code": "LOCKED", "message": "cannot touch"}
        raise AssertionError(tool)

    ns, _calls = _exec_generated_proxy(_bookmark_tools(), _handler)
    bm = ns["bookmark"]

    with pytest.raises(RuntimeError, match="cannot touch") as exc_info:
        _ = bm["A"]
    assert getattr(exc_info.value, "code", None) == "LOCKED"

    with pytest.raises(RuntimeError, match="cannot touch") as del_exc:
        del bm["A"]
    assert getattr(del_exc.value, "code", None) == "LOCKED"

    list_fails = True
    with pytest.raises(RuntimeError, match="cannot touch") as fallback_exc:
        _ = bm["Z"]
    assert getattr(fallback_exc.value, "code", None) == "LOCKED"


def test_collection_without_get_tool_reads_item_from_list():
    tools = [
        _as_tool(MockTool("sheet_list", "List sheets.", {"type": "object", "properties": {}}, specialized_domain="sheets")),
        _as_tool(
            MockTool(
                "sheet_delete",
                "Delete sheet.",
                {"type": "object", "properties": {"sheet": {"type": "string"}}, "required": ["sheet"]},
                specialized_domain="sheets",
            )
        ),
    ]

    def _handler(tool: str, kwargs: dict[str, object]):
        if tool == "sheet_list":
            return {"status": "ok", "result": [{"name": "S1", "visible": True}, {"name": "S2", "visible": False}]}
        if tool == "sheet_delete":
            return {"status": "ok"}
        raise AssertionError(tool)

    ns, calls = _exec_generated_proxy(tools, _handler)
    sheet = ns["sheet"]

    assert sheet["S2"].visible is False
    assert calls == [("sheet_list", {})]

    with pytest.raises(KeyError):
        _ = sheet["Missing"]

    del sheet["S1"]
    assert calls[-1] == ("sheet_delete", {"sheet": "S1"})


def test_collection_drops_optional_arguments():
    tools = [
        _as_tool(
            MockTool(
                "pivot_table_list",
                "List pivots.",
                {"type": "object", "properties": {"sheet": {"type": "string"}}},
                specialized_domain="pivot_tables",
            )
        ),
        _as_tool(
            MockTool(
                "pivot_table_delete",
                "Delete pivot.",
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "sheet": {"type": "string"}},
                    "required": ["name"],
                },
                specialized_domain="pivot_tables",
            )
        ),
    ]

    def _handler(tool: str, kwargs: dict[str, object]):
        if tool == "pivot_table_list":
            return {"status": "ok", "pivot_tables": [{"name": "P", "sheet": "S1"}], "count": 1}
        if tool == "pivot_table_delete":
            return {"status": "ok"}
        raise AssertionError(tool)

    ns, calls = _exec_generated_proxy(tools, _handler)
    pt = ns["pivot_table"]

    assert [item.name for item in pt] == ["P"]
    del pt["P"]
    assert calls == [("pivot_table_list", {}), ("pivot_table_delete", {"name": "P"})]


@pytest.mark.parametrize(
    "bad_tools",
    [
        # No delete tool
        [MockTool("bookmark_list", "List.", {"type": "object", "properties": {}})],
        # Two required delete parameters
        [
            MockTool("bookmark_list", "List.", {"type": "object", "properties": {}}),
            MockTool(
                "bookmark_delete",
                "Delete.",
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "scope": {"type": "string"}},
                    "required": ["name", "scope"],
                },
            ),
        ],
        # Integer key
        [
            MockTool("bookmark_list", "List.", {"type": "object", "properties": {}}),
            MockTool(
                "bookmark_delete",
                "Delete.",
                {"type": "object", "properties": {"index": {"type": "integer"}}, "required": ["index"]},
            ),
        ],
        # Required list parameter
        [
            MockTool(
                "bookmark_list",
                "List.",
                {"type": "object", "properties": {"scope": {"type": "string"}}, "required": ["scope"]},
            ),
            MockTool(
                "bookmark_delete",
                "Delete.",
                {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            ),
        ],
        # Get key different from delete key
        [
            MockTool("bookmark_list", "List.", {"type": "object", "properties": {}}),
            MockTool(
                "bookmark_get",
                "Get.",
                {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            ),
            MockTool(
                "bookmark_delete",
                "Delete.",
                {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            ),
        ],
    ],
)
def test_collection_namespace_shape_errors(bad_tools):
    with pytest.raises(ValueError, match="bookmark"):
        generate_module([_as_tool(t) for t in bad_tools])


def test_collection_namespace_rejects_paging_parameter():
    tools = [
        _as_tool(
            MockTool(
                "bookmark_list",
                "List.",
                {"type": "object", "properties": {"start_index": {"type": "integer"}}},
            )
        ),
        _as_tool(
            MockTool(
                "bookmark_delete",
                "Delete.",
                {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            )
        ),
    ]
    with pytest.raises(ValueError, match="paging parameter"):
        generate_module(tools)


def test_paging_start_param_rejects_required_parameters():
    tools = [
        _as_tool(
            MockTool(
                "comment_list",
                "List.",
                {
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string"},
                        "start_index": {"type": "integer"},
                    },
                    "required": ["scope"],
                },
            )
        ),
    ]
    with pytest.raises(ValueError, match="must not have required parameters"):
        generate_module(tools)


def test_collection_rejects_non_dict_list_entries():
    ns, _calls = _exec_generated_proxy(
        _bookmark_tools(),
        lambda tool, kwargs: {"status": "ok", "bookmarks": ["A"], "count": 1},
    )
    bm = ns["bookmark"]
    with pytest.raises(RuntimeError, match="Expected dict entries"):
        _ = "A" in bm
    with pytest.raises(RuntimeError, match="Expected dict entries"):
        list(bm)


def test_flat_method_raises_tool_error_with_code():
    def _handler(tool: str, kwargs: dict[str, object]):
        if kwargs.get("text") == "bad":
            return {"status": "error", "code": "LOCKED", "message": "cannot touch"}
        return "oops"

    ns, _calls = _exec_generated_proxy(
        [
            _as_tool(
                MockTool(
                    "footnote_insert",
                    "Insert.",
                    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                )
            )
        ],
        _handler,
    )
    fn = ns["footnote"]
    with pytest.raises(RuntimeError, match="cannot touch") as exc_info:
        fn.insert("bad")
    assert getattr(exc_info.value, "code", None) == "LOCKED"

    with pytest.raises(RuntimeError, match="oops"):
        fn.insert("non_dict")


def test_flat_method_user_stopped_dict_raises_user_stopped():
    from plugin.framework.ipc import UserStopped

    ns, _calls = _exec_generated_proxy(
        [
            _as_tool(
                MockTool(
                    "web_research",
                    "Research.",
                    {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
                )
            )
        ],
        lambda tool, kwargs: {"status": "error", "code": "USER_STOPPED", "message": "stopped by user"},
    )
    with pytest.raises(UserStopped, match="stopped by user"):
        try:
            ns["web"].research("hello")
        except Exception as exc:
            raise AssertionError(f"UserStopped must not be caught by except Exception: {exc}") from exc


def test_flat_method_result_reads_as_attributes():
    def _handler(tool: str, kwargs: dict[str, object]):
        if kwargs.get("text") == "skip":
            return {"status": "skipped", "message": "already exists"}
        return {"status": "ok", "text": "hi"}

    ns, _calls = _exec_generated_proxy(
        [
            _as_tool(
                MockTool(
                    "footnote_insert",
                    "Insert.",
                    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                )
            )
        ],
        _handler,
    )
    fn = ns["footnote"]
    r = fn.insert("ok")
    assert r.text == r["text"] == "hi"
    assert "status" not in r
    with pytest.raises(AttributeError):
        _ = r.missing_key

    skipped = fn.insert("skip")
    assert skipped.status == "skipped"
    assert skipped.message == "already exists"


def test_paged_list_iterates_lazily():
    rows = [{"name": f"c{i}", "content": f"body{i}"} for i in range(5)]

    def _handler(tool: str, kwargs: dict[str, object]):
        assert tool == "comment_list"
        start = int(kwargs.get("start_index") or 0)
        page = rows[start : start + 2]
        next_idx = start + len(page)
        has_more = next_idx < len(rows)
        return {
            "status": "ok",
            "comments": page,
            "has_more": has_more,
            "next_index": next_idx if has_more else None,
        }

    tools = [
        _as_tool(
            MockTool(
                "comment_list",
                "List comments.",
                {
                    "type": "object",
                    "properties": {
                        "author_filter": {"type": "string"},
                        "start_index": {"type": "integer"},
                        "max_comments": {"type": "integer"},
                    },
                },
            )
        )
    ]
    ns, calls = _exec_generated_proxy(tools, _handler)

    it = iter(ns["comment"])
    assert calls == []

    first = next(it)
    assert first.name == "c0"
    assert calls == [("comment_list", {"start_index": 0})]

    second = next(it)
    assert second.name == "c1"
    assert len(calls) == 1

    third = next(it)
    assert third.name == "c2"
    assert len(calls) == 2

    rest = [c.name for c in it]
    assert rest == ["c3", "c4"]
    assert [kw["start_index"] for _tool, kw in calls] == [0, 2, 4]

    one_page = ns["comment"].list(start_index=2)
    assert one_page.has_more is True
    assert [c["name"] for c in one_page.comments] == ["c2", "c3"]
    assert ns["TOOL_METHODS"]["comment_list"] == ("comment", "list")


def test_paged_offset_list_follows_truncated():
    rows = [{"content": f"p{i}"} for i in range(5)]

    def _handler(tool: str, kwargs: dict[str, object]):
        assert tool == "page_comment_list"
        offset = int(kwargs.get("offset") or 0)
        page = rows[offset : offset + 2]
        truncated = offset + len(page) < len(rows)
        return {"status": "ok", "comments": page, "truncated": truncated, "offset": offset, "count": len(page)}

    tools = [
        _as_tool(
            MockTool(
                "page_comment_list",
                "List page comments.",
                {"type": "object", "properties": {"page": {"type": "integer"}, "offset": {"type": "integer"}}},
            )
        )
    ]
    ns, calls = _exec_generated_proxy(tools, _handler)
    assert [c.content for c in ns["page_comment"]] == ["p0", "p1", "p2", "p3", "p4"]
    assert [kw["offset"] for _tool, kw in calls] == [0, 2, 4]


def test_paged_iteration_raises_on_later_page_error():
    def _handler(tool: str, kwargs: dict[str, object]):
        if kwargs.get("start_index") == 0:
            return {"status": "ok", "comments": [{"name": "c0"}], "has_more": True, "next_index": 1}
        return {"status": "error", "code": "LIST_FAIL", "message": "page 2 failed"}

    tools = [
        _as_tool(
            MockTool(
                "comment_list",
                "List comments.",
                {"type": "object", "properties": {"start_index": {"type": "integer"}}},
            )
        )
    ]
    ns, _calls = _exec_generated_proxy(tools, _handler)
    it = iter(ns["comment"])
    assert next(it).name == "c0"
    with pytest.raises(RuntimeError, match="page 2 failed") as exc_info:
        next(it)
    assert getattr(exc_info.value, "code", None) == "LIST_FAIL"

