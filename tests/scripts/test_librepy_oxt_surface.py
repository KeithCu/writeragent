"""Hermetic LibrePy OXT surface contract: required/forbidden remapped archive entries."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.build_librepy_oxt import (  # noqa: E402
    LIBREPY_DIALOG_FILES,
    LIBREPY_GENERATED_DIALOG_FILES,
    iter_librepy_archive_entries,
    librepy_include_paths,
    stage_librepy_plugin_tree,
)

_REQUIRED_RUNTIME = (
    "plugin/main_core.py",
    "plugin/librepy/sidebar_menus.py",
    "plugin/calc/python/addin_librepy.py",
    "plugin/calc/python/workbook_lifecycle.py",
    "plugin/scripting/venv_worker.py",
    "XPythonFunction.rdb",
    "Jobs.xcu",
    "ProtocolHandler.xcu",
    "registry/org/openoffice/Office/CalcAddIns.xcu",
    "registry/org/openoffice/Office/UI/Sidebar.xcu",
    "registry/org/openoffice/Office/UI/Factories.xcu",
    "plugin/notebook/import_filter.py",
    "registry/org/openoffice/TypeDetection/Types.xcu",
    "registry/org/openoffice/TypeDetection/Filters.xcu",
    "registry/org/openoffice/TypeDetection/Misc.xcu",
)

_REQUIRED_UI = (
    "Dialogs/PythonScriptDialog.xdl",
    "Dialogs/PythonCellEditorDialog.xdl",
    "Dialogs/PythonTestProgressDialog.xdl",
    "Dialogs/PythonSidebarDialog.xdl",
    "Dialogs/TextAnalyticsDialog.xdl",
    "Dialogs/LatexInputDialog.xdl",
    "Dialogs/MsgBoxWithCopyDialog.xdl",
    "Dialogs/ErrorReportDialog.xdl",
)

_FORBIDDEN_RUNTIME = (
    "plugin/main.py",
    "plugin/calc/python/addin.py",
    "plugin/calc/prompt_addin.py",
    "plugin/calc/prompt_function.py",
    "plugin/framework/client/llm_client.py",
    "plugin/embeddings/",
    "plugin/mcp/",
    "XPromptFunction.rdb",
)

_FORBIDDEN_UI = (
    "Dialogs/ChatPanelDialog.xdl",
    "Dialogs/SearchDialog.xdl",
    "Dialogs/EvalDialog.xdl",
    "Dialogs/SpreadsheetImportDialog.xdl",
    "Dialogs/WebSearchQueryEditDialog.xdl",
    "Dialogs/ServerStatusDialog.xdl",
)


def _entries() -> set[str]:
    return set(iter_librepy_archive_entries(str(_REPO_ROOT)))


def test_include_list_uses_explicit_dialogs_not_wholesale_trees():
    include = librepy_include_paths(str(_REPO_ROOT), include_locales=False)
    assert "extension/Dialogs/" not in include
    assert "build/generated/Dialogs/" not in include
    for rel in LIBREPY_DIALOG_FILES:
        assert rel in include
    for rel in LIBREPY_GENERATED_DIALOG_FILES:
        assert rel in include


def test_archive_entries_include_required_runtime_and_ui():
    entries = _entries()
    missing = [path for path in _REQUIRED_RUNTIME + _REQUIRED_UI if path not in entries]
    assert missing == []


def test_archive_entries_exclude_writeragent_surfaces():
    entries = _entries()
    leaked = [path for path in _FORBIDDEN_RUNTIME + _FORBIDDEN_UI if path in entries]
    leaked.extend(path for path in entries if path.startswith("plugin/embeddings/"))
    leaked.extend(path for path in entries if path.startswith("plugin/mcp/"))
    assert leaked == []


def test_generated_settings_dialog_included_when_present():
    entries = _entries()
    settings = _REPO_ROOT / "build" / "generated" / "Dialogs" / "SettingsDialog.xdl"
    vision = _REPO_ROOT / "build" / "generated" / "Dialogs" / "VisionSettingsDialog.xdl"
    if settings.is_file():
        assert "Dialogs/SettingsDialog.xdl" in entries
    if vision.is_file():
        assert "Dialogs/VisionSettingsDialog.xdl" in entries


def test_archive_entry_set_is_deterministic():
    first = iter_librepy_archive_entries(str(_REPO_ROOT))
    second = iter_librepy_archive_entries(str(_REPO_ROOT))
    assert first == second


def test_plugin_stage_is_deterministic(tmp_path: Path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    stage_librepy_plugin_tree(str(_REPO_ROOT), str(first))
    stage_librepy_plugin_tree(str(_REPO_ROOT), str(second))

    def _rel_files(root: Path) -> set[str]:
        return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}

    assert _rel_files(first) == _rel_files(second)
    assert "plugin/calc/python/workbook_lifecycle.py" in _rel_files(first)
    assert "plugin/main.py" not in _rel_files(first)


def test_identity_sources_are_librepy_not_prompt():
    jobs = (_REPO_ROOT / "extension-core" / "Jobs.xcu").read_text(encoding="utf-8")
    assert "org.extension.librepy.Main" in jobs
    assert "org.extension.writeragent.Main" not in jobs

    addins = (
        _REPO_ROOT / "extension-core" / "registry" / "org" / "openoffice" / "Office" / "CalcAddIns.xcu"
    ).read_text(encoding="utf-8")
    assert 'oor:name="py"' in addins
    assert 'oor:name="python"' in addins
    assert "prompt" not in addins.lower()


def test_staged_plugin_imports_resolve_to_stage(tmp_path: Path):
    staged = tmp_path / "staged"
    stage_librepy_plugin_tree(str(_REPO_ROOT), str(staged))
    script = r"""
import sys
import types

staged = sys.argv[1]
sys.path.insert(0, staged)

uno = types.ModuleType("uno")
unohelper = types.ModuleType("unohelper")

class _Base:
    pass

class _ImplementationHelper:
    def addImplementation(self, *args, **kwargs):
        return None

unohelper.Base = _Base
unohelper.ImplementationHelper = _ImplementationHelper
sys.modules["uno"] = uno
sys.modules["unohelper"] = unohelper
for name in (
    "com",
    "com.sun",
    "com.sun.star",
    "com.sun.star.frame",
    "com.sun.star.lang",
    "com.sun.star.task",
    "com.sun.star.util",
):
    sys.modules.setdefault(name, types.ModuleType(name))

def _iface(mod_name, attr):
    cls = type(attr, (), {})
    setattr(sys.modules[mod_name], attr, cls)

_iface("com.sun.star.frame", "DispatchDescriptor")
_iface("com.sun.star.frame", "XDispatch")
_iface("com.sun.star.frame", "XDispatchProvider")
_iface("com.sun.star.frame", "XTerminateListener")
_iface("com.sun.star.lang", "XInitialization")
_iface("com.sun.star.lang", "XServiceInfo")
_iface("com.sun.star.task", "XJob")
_iface("com.sun.star.task", "XJobExecutor")
_iface("com.sun.star.util", "XModifyListener")
_iface("com.sun.star.util", "XCloseListener")

import plugin.calc.python.function as fn
import plugin.calc.python.workbook_lifecycle as wl
import plugin.main_core as mc

def _under(path):
    return os.path.realpath(path).startswith(os.path.realpath(staged) + os.sep)

import os
assert _under(fn.__file__), fn.__file__
assert _under(wl.__file__), wl.__file__
assert _under(mc.__file__), mc.__file__
"""
    proc = subprocess.run(
        [sys.executable, "-c", script, str(staged)],
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(staged)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_missing_generated_settings_dialog_fails_assembly(monkeypatch, tmp_path: Path):
    from scripts.build_librepy_oxt import assemble_librepy_bundle

    monkeypatch.setattr("scripts.build_librepy_oxt._ensure_librepy_manifest", lambda base_dir: None)
    monkeypatch.setattr("scripts.build_librepy_oxt.patch_description_xml", lambda dest: None)
    monkeypatch.setattr("scripts.build_librepy_oxt.generate_update_xml", lambda base, name: None)

    real_isfile = os.path.isfile

    def fake_isfile(path: str) -> bool:
        if "SettingsDialog.xdl" in path:
            return False
        return real_isfile(path)

    monkeypatch.setattr("os.path.isfile", fake_isfile)
    assert assemble_librepy_bundle(str(_REPO_ROOT), bundle_dir=str(tmp_path / "bundle")) == 0


def test_librepy_bundle_includes_latex2mathml_metadata(tmp_path: Path):
    from scripts.build_oxt import sync_vendor_into_lib
    from scripts.librepy_bundle_paths import LIBREPY_VENDOR_PACKAGES

    vendor_dir = str(_REPO_ROOT / "vendor")
    lib_dir = tmp_path / "plugin" / "lib"
    sync_vendor_into_lib(
        vendor_dir,
        str(lib_dir),
        prune_websockets=False,
        only=LIBREPY_VENDOR_PACKAGES,
    )
    meta_files = list(lib_dir.glob("latex2mathml-*.dist-info/METADATA"))
    assert len(meta_files) == 1
    assert meta_files[0].is_file()
