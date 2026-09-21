"""LibrePy allowlist must cover top-level plugin imports of shipped modules."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.build_librepy_oxt import stage_librepy_plugin_tree  # noqa: E402
from scripts.librepy_bundle_paths import (  # noqa: E402
    LIBREPY_VENDOR_PACKAGES,
    collect_librepy_plugin_paths,
)

# Lazy attrs on plugin.framework.client that load modules not in the LibrePy OXT.
_FORBIDDEN_CLIENT_ATTRS = frozenset(
    {
        "LlmClient",
        "OPENROUTER_CHAT_EXTRA_BLOCKLIST",
        "merge_openrouter_chat_extra",
        "strip_leaked_chat_template_control_tokens",
        "EmbeddingBatch",
        "embed_texts",
        "get_embedding_model",
        "delete_paragraphs",
        "index_paragraphs",
        "knn_search",
        "iterate_sse",
        "run_trusted_analysis",
    }
)


def _get_shipped_paths() -> set[str]:
    return set(collect_librepy_plugin_paths(str(_REPO_ROOT)))


def _plugin_mod_to_candidates(mod: str) -> tuple[str, ...]:
    rel = mod.replace(".", "/")
    return (rel + ".py", rel + "/__init__.py")


def _is_shipped_plugin_module(mod: str, shipped: set[str]) -> bool:
    if mod in ("plugin._manifest", "plugin._manifest_librepy"):
        return True
    cands = _plugin_mod_to_candidates(mod)
    return any(c in shipped for c in cands)


def _is_type_checking_if(node: ast.If) -> bool:
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


def _module_level_import_nodes(tree: ast.AST) -> list[ast.AST]:
    nodes: list[ast.AST] = []

    def walk(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                nodes.append(stmt)
            elif isinstance(stmt, ast.If):
                if _is_type_checking_if(stmt):
                    continue
                walk(stmt.body)
                walk(stmt.orelse)
            elif isinstance(stmt, ast.Try):
                handler_names = []
                for handler in stmt.handlers:
                    if handler.type is None:
                        handler_names.append("bare")
                    elif isinstance(handler.type, ast.Name):
                        handler_names.append(handler.type.id)
                    elif isinstance(handler.type, ast.Tuple):
                        handler_names.extend(
                            elt.id for elt in handler.type.elts if isinstance(elt, ast.Name)
                        )
                # Optional imports (Cython accelerator, generated manifest, etc.)
                if "ImportError" in handler_names or "ModuleNotFoundError" in handler_names:
                    walk(stmt.orelse)
                    walk(stmt.finalbody)
                    continue
                walk(stmt.body)
                for handler in stmt.handlers:
                    walk(handler.body)
                walk(stmt.orelse)
                walk(stmt.finalbody)
            elif isinstance(stmt, ast.With):
                walk(stmt.body)

    walk(tree.body)
    return nodes


def _imported_plugin_modules(path: Path, tree: ast.AST) -> list[str]:
    pkg_parts = path.relative_to(_REPO_ROOT).with_suffix("").parts
    parent_pkg = pkg_parts[:-1]
    found: list[str] = []
    for node in _module_level_import_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "plugin" or alias.name.startswith("plugin."):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level > len(parent_pkg):
                    continue
                rel_base = parent_pkg[: len(parent_pkg) - node.level + 1]
                if node.module:
                    mods = [".".join(rel_base + tuple(node.module.split(".")))]
                else:
                    mods = [".".join(rel_base + (alias.name,)) for alias in node.names]
            else:
                mods = [node.module] if node.module else []
            for mod in mods:
                if mod == "plugin" or mod.startswith("plugin."):
                    found.append(mod)
    return found


def _resolved_import_from_module(path: Path, node: ast.ImportFrom) -> str | None:
    pkg_parts = path.relative_to(_REPO_ROOT).with_suffix("").parts
    parent_pkg = pkg_parts[:-1]
    if node.level:
        if node.level > len(parent_pkg):
            return None
        rel_base = parent_pkg[: len(parent_pkg) - node.level + 1]
        if node.module:
            return ".".join(rel_base + tuple(node.module.split(".")))
        return ".".join(rel_base)
    return node.module


def _forbidden_client_attr_hits(path: Path, tree: ast.AST) -> list[str]:
    """``from plugin.framework.client import LlmClient`` is not a llm_client prefix hit."""
    hits: list[str] = []
    for node in _module_level_import_nodes(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if _resolved_import_from_module(path, node) != "plugin.framework.client":
            continue
        if node.names and node.names[0].name == "*":
            hits.append("*")
            continue
        for alias in node.names:
            if alias.name in _FORBIDDEN_CLIENT_ATTRS:
                hits.append(alias.name)
    return hits


def test_librepy_bundle_includes_xl_static_rewrite_and_addin_impl():
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/calc/python/xl_static_rewrite.py" in paths
    assert "plugin/calc/python/addin_impl.py" in paths
    assert "plugin/scripting/native_binaries.py" in paths
    assert "plugin/scripting/audio_recorder_service.py" not in paths
    assert "plugin/calc/python/workbook_lifecycle.py" in paths


def test_unbundled_plugin_modules_are_strictly_excluded():
    all_plugin_py = {
        p.relative_to(_REPO_ROOT).as_posix()
        for p in (_REPO_ROOT / "plugin").rglob("*.py")
    }
    shipped = _get_shipped_paths()
    unbundled = all_plugin_py - shipped
    assert len(unbundled) > 100
    # Spot-check essential unbundled modules (automatically forbidden by allowlist complement)
    assert "plugin/doc/document_helpers.py" in unbundled
    assert "plugin/doc/common_module.py" in unbundled
    assert "plugin/framework/client/llm_client.py" in unbundled
    assert "plugin/framework/prompts.py" in unbundled
    assert "plugin/framework/tool.py" in unbundled


def test_librepy_shipped_toplevel_plugin_imports_are_bundled():
    shipped = _get_shipped_paths()
    missing: list[str] = []
    for rel in sorted(shipped):
        if not rel.endswith(".py"):
            continue
        path = _REPO_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for attr in _forbidden_client_attr_hits(path, tree):
            missing.append(f"{rel} top-level-imports-forbidden plugin.framework.client.{attr}")
        for mod in _imported_plugin_modules(path, tree):
            if rel == "plugin/contrib/smolagents/__init__.py":
                # build_librepy_oxt.py replaces this with a slim stub.
                continue
            if not _is_shipped_plugin_module(mod, shipped):
                missing.append(f"{rel} -> {mod} (not in LibrePy allowlist)")
    assert missing == []


def test_from_client_import_llmclient_is_forbidden():
    dummy = _REPO_ROOT / "plugin" / "librepy" / "settings.py"
    tree = ast.parse("from plugin.framework.client import LlmClient, sync_request")
    assert _forbidden_client_attr_hits(dummy, tree) == ["LlmClient"]
    tree_ok = ast.parse("from plugin.framework.client import sync_request")
    assert _forbidden_client_attr_hits(dummy, tree_ok) == []


def test_librepy_entry_imports_avoid_writeragent_only_modules():
    from plugin.tests.testing_utils import setup_uno_mocks

    setup_uno_mocks()
    shipped = _get_shipped_paths()
    before = set(sys.modules)

    import plugin.calc.python.addin_impl  # noqa: F401
    import plugin.calc.python.addin_librepy  # noqa: F401
    import plugin.calc.python.cell_discovery  # noqa: F401
    import plugin.calc.python.diagnostics  # noqa: F401
    import plugin.calc.python.editor  # noqa: F401
    import plugin.calc.python.editor_context_menu  # noqa: F401
    import plugin.calc.python.formula_edit  # noqa: F401
    import plugin.calc.python.function  # noqa: F401
    import plugin.calc.python.image_egress  # noqa: F401
    import plugin.calc.python.init_script_editor  # noqa: F401
    import plugin.calc.python.workbook_lifecycle  # noqa: F401
    import plugin.calc.python.xl_static_rewrite  # noqa: F401
    import plugin.librepy.panel_factory  # noqa: F401
    import plugin.librepy.python_sidebar  # noqa: F401
    import plugin.librepy.sidebar_menus  # noqa: F401
    import plugin.librepy.settings  # noqa: F401
    import plugin.scripting.python_runner  # noqa: F401
    import plugin.scripting.trusted_action_registry  # noqa: F401
    import plugin.scripting.venv_worker  # noqa: F401
    import plugin.vision.vision_availability  # noqa: F401
    import plugin.vision.vision_runner  # noqa: F401
    import plugin.writer.format  # noqa: F401
    import plugin.writer.images.image_tools  # noqa: F401
    import plugin.writer.math.latex_dialog  # noqa: F401
    import plugin.writer.xhtml_style_postprocess  # noqa: F401
    import plugin.notebook.import_filter  # noqa: F401
    import plugin.notebook.writer_importer  # noqa: F401

    loaded = set(sys.modules) - before
    bad: list[str] = []
    for name in sorted(loaded):
        if name == "plugin" or name in ("plugin._manifest", "plugin._manifest_librepy"):
            continue
        if name.startswith("plugin."):
            mod_obj = sys.modules.get(name)
            file_path = getattr(mod_obj, "__file__", None)
            if file_path:
                try:
                    rel = Path(file_path).resolve().relative_to(_REPO_ROOT).as_posix()
                    if rel not in shipped:
                        bad.append(f"{name} -> {rel}")
                except ValueError:
                    pass
            elif not _is_shipped_plugin_module(name, shipped):
                bad.append(name)
    assert bad == []


def test_librepy_shipped_function_level_imports_are_safe():
    """Verify that any function-level import of unbundled modules in shipped files is safely guarded."""
    shipped = _get_shipped_paths()
    known_special_cases = {
        # Vendored upstream smolagents helper (unused in LibrePy runtime; executor uses AST helpers)
        "plugin/contrib/smolagents/utils.py",
        # WriterAgent-only API helper in config (get_api_config for LlmClient)
        "plugin/framework/config.py",
        # Venv worker trusted dispatch for domains only registered in WriterAgent
        "plugin/scripting/venv/trusted_dispatch.py",
        "plugin/scripting/venv/worker_harness.py",
        # Image insert page style resolution
        "plugin/writer/images/image_tools.py",
        # Writer scope selection fallback
        "plugin/writer/format.py",
        # Nested-table refuse (master ca0fc04f) imports WriterAgent-only specialized.tables.
        # Not this harness PR; same allowlist pattern as format.py.
        "plugin/writer/html_import.py",
        # HTML table copy now imports WriterAgent-only specialized.tables
        # (same pattern as html_import.py).
        "plugin/writer/html_export.py",
    }

    def is_guarded(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
        curr = node
        while curr in parents:
            par = parents[curr]
            if isinstance(par, ast.If):
                test = par.test
                if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                    isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
                ):
                    if curr in par.body:
                        return True
            elif isinstance(par, ast.Try):
                if curr in par.body:
                    handler_names = []
                    for h in par.handlers:
                        if h.type is None:
                            handler_names.append("bare")
                        elif isinstance(h.type, ast.Name):
                            handler_names.append(h.type.id)
                        elif isinstance(h.type, ast.Tuple):
                            handler_names.extend(
                                elt.id for elt in h.type.elts if isinstance(elt, ast.Name)
                            )
                    if any(
                        n in ("ImportError", "ModuleNotFoundError", "Exception", "BaseException", "bare")
                        for n in handler_names
                    ):
                        return True
            curr = par
        return False

    unexpected: list[tuple[str, int, str]] = []
    for rel in sorted(shipped):
        if not rel.endswith(".py") or rel == "plugin/contrib/smolagents/__init__.py":
            continue
        if rel in known_special_cases:
            continue
        path = _REPO_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        pkg_parts = Path(rel).with_suffix("").parts[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("plugin."):
                        if not _is_shipped_plugin_module(alias.name, shipped):
                            if not is_guarded(node, parents):
                                unexpected.append((rel, node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                level = node.level
                mod = node.module or ""
                if level > 0:
                    if level <= len(pkg_parts):
                        base = pkg_parts[: len(pkg_parts) - level + 1]
                        full_mod = ".".join(base + (tuple(mod.split(".")) if mod else ()))
                    else:
                        full_mod = None
                else:
                    full_mod = mod if mod.startswith("plugin.") else None
                if full_mod and full_mod.startswith("plugin."):
                    if not _is_shipped_plugin_module(full_mod, shipped):
                        sub_cands = [
                            _plugin_mod_to_candidates(f"{full_mod}.{alias.name}")
                            for alias in node.names
                        ]
                        if not any(any(sc in shipped for sc in scands) for scands in sub_cands):
                            if not is_guarded(node, parents):
                                unexpected.append((rel, node.lineno, full_mod))

    assert unexpected == []


def test_librepy_staged_bundle_imports_without_writeragent(tmp_path: Path):
    """Verify that every shipped LibrePy module imports cleanly in an isolated tree."""
    import shutil

    staged = tmp_path / "staged"
    stage_librepy_plugin_tree(str(_REPO_ROOT), str(staged))

    # Provide slim manifest for testing
    (staged / "plugin" / "_manifest.py").write_text(
        '"""Slim LibrePy test manifest."""\nMODULES = {}\n',
        encoding="utf-8",
    )

    # Vendor packages into plugin/lib
    vendor_dir = _REPO_ROOT / "vendor"
    lib_dir = staged / "plugin" / "lib"
    for pkg in LIBREPY_VENDOR_PACKAGES:
        src = vendor_dir / pkg
        if src.is_dir():
            shutil.copytree(src, lib_dir / pkg)

    # Isolate subprocess to staged tree without editable finder hooks
    script = r"""
import sys, types, os, importlib
from pathlib import Path

staged = sys.argv[1]
# Clear editable finder hooks and site-packages to ensure clean hermetic evaluation
sys.meta_path = [
    finder for finder in sys.meta_path
    if "editable" not in finder.__class__.__name__.lower() and "writeragent" not in str(finder).lower()
]
sys.path_hooks = [
    h for h in sys.path_hooks
    if "editable" not in h.__class__.__name__.lower() and "writeragent" not in str(h).lower()
]
sys.path = [staged, os.path.join(staged, "plugin", "lib")] + [
    p for p in sys.path if "writeragent" not in p and "site-packages" not in p
]

# Set up UNO mocks in the isolated child process
uno = types.ModuleType("uno")
unohelper = types.ModuleType("unohelper")
class _Base: pass
class _ImplementationHelper:
    def addImplementation(self, *args, **kwargs): return None
unohelper.Base = _Base
unohelper.ImplementationHelper = _ImplementationHelper
sys.modules["uno"] = uno
sys.modules["unohelper"] = unohelper

for name in (
    "com", "com.sun", "com.sun.star",
    "com.sun.star.awt", "com.sun.star.awt.grid", "com.sun.star.awt.tree",
    "com.sun.star.beans", "com.sun.star.container", "com.sun.star.datatransfer",
    "com.sun.star.datatransfer.clipboard",
    "com.sun.star.document", "com.sun.star.drawing", "com.sun.star.frame",
    "com.sun.star.lang", "com.sun.star.sheet", "com.sun.star.style",
    "com.sun.star.table", "com.sun.star.task", "com.sun.star.text",
    "com.sun.star.text.TextContentAnchorType",
    "com.sun.star.ui", "com.sun.star.uno", "com.sun.star.util", "com.sun.star.view"
):
    mod = types.ModuleType(name)
    mod.__path__ = []
    sys.modules[name] = mod

for name in list(sys.modules):
    if "." in name and name.startswith("com."):
        parent_name, child_name = name.rsplit(".", 1)
        if parent_name in sys.modules:
            setattr(sys.modules[parent_name], child_name, sys.modules[name])

def _iface(mod_name, attr):
    cls = type(attr, (), {})
    setattr(sys.modules[mod_name], attr, cls)

for mod_name in list(sys.modules):
    if mod_name.startswith("com.sun.star"):
        for attr in (
            "AS_CHARACTER", "AT_CHARACTER", "AT_PARAGRAPH", "AT_PAGE", "AT_FRAME",
            "DisposedException", "NoSuchElementException", "IllegalArgumentException",
            "RuntimeException", "Exception",
            "XDispatch", "XDispatchProvider", "XInitialization", "XServiceInfo",
            "XJob", "XJobExecutor", "XModifyListener", "XCloseListener",
            "XTerminateListener", "DispatchDescriptor", "PropertyValue", "NamedValue",
            "XControlModel", "XControl", "XWindow", "XActionListener", "XItemListener",
            "XTextListener", "XTextComponent", "XListBox", "XComboBox", "XCheckBox", "XRadioButton",
            "XDialog", "XDialogProvider2", "XTopWindow", "XTopWindowListener",
            "XSidebarPanel", "XToolPanel", "XUIElement", "XUIElementFactory",
            "Size", "Point"
        ):
            _iface(mod_name, attr)

paths = sys.argv[2:]
errors = []
for p in paths:
    if p.endswith(".py"):
        rel = p[:-3].replace("/", ".")
        if rel.endswith(".__init__"):
            rel = rel[:-9]
        try:
            importlib.import_module(rel)
        except Exception as e:
            errors.append((rel, f"{type(e).__name__}: {e}"))

if errors:
    print(f"FAILED_IMPORTS: {len(errors)}")
    for rel, err in errors:
        print(f"  {rel}: {err}")
    sys.exit(1)
"""
    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    proc = subprocess.run(
        [sys.executable, "-c", script, str(staged), *paths],
        cwd=str(staged),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr

