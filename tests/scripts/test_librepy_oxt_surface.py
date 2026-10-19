"""Hermetic LibrePy OXT surface contract: required/forbidden remapped archive entries."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.build_librepy_oxt import (  # noqa: E402
    LIBREPY_DIALOG_FILES,
    LIBREPY_GENERATED_DIALOG_FILES,
    _FORBIDDEN_RUNTIME,
    _FORBIDDEN_UI,
    _REQUIRED_RUNTIME,
    _REQUIRED_UI,
    BuildError,
    iter_librepy_archive_entries,
    librepy_include_paths,
    librepy_manifest_cmd,
    stage_librepy_plugin_tree,
    verify_librepy_archive,
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


def _create_synthetic_zip(
    zip_path: Path,
    *,
    omit_metadata: bool = False,
    extra_entries: dict[str, str] | None = None,
    description_version: str | None = None,
    manifest_entries: list[str] | None = None,
) -> None:
    import zipfile
    from plugin.version import EXTENSION_VERSION

    version = description_version or EXTENSION_VERSION
    manifest_fps = manifest_entries if manifest_entries is not None else ["plugin/main_core.py"]

    manifest_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest xmlns:manifest="http://openoffice.org/2001/manifest">\n'
        + "".join(
            '  <manifest:file-entry manifest:media-type="text/plain" manifest:full-path="%s" />\n' % fp
            for fp in manifest_fps
        )
        + "</manifest:manifest>\n"
    )

    desc_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<description xmlns="http://openoffice.org/extensions/description/2006">\n'
        '  <version value="%s"/>\n'
        "</description>\n" % version
    )

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("plugin/_manifest.py", "#\n")
        zf.writestr("plugin/lib/isodate/__init__.py", "#\n")
        zf.writestr("plugin/lib/json_repair/__init__.py", "#\n")
        zf.writestr("plugin/lib/latex2mathml/__init__.py", "#\n")
        zf.writestr("plugin/lib/mathml_to_latex/__init__.py", "#\n")
        if not omit_metadata:
            zf.writestr("plugin/lib/latex2mathml-3.81.0.dist-info/METADATA", "Name: latex2mathml\n")

        for req in _REQUIRED_RUNTIME:
            zf.writestr(req, "#\n")
        for req in _REQUIRED_UI:
            zf.writestr(req, "<dlg/>\n")

        zf.writestr("META-INF/manifest.xml", manifest_xml)
        zf.writestr("description.xml", desc_xml)

        if extra_entries:
            for arcname, data in extra_entries.items():
                zf.writestr(arcname, data)


def test_verify_librepy_archive_valid(tmp_path: Path):
    zip_path = tmp_path / "valid.oxt"
    _create_synthetic_zip(zip_path)
    verify_librepy_archive(str(zip_path), with_tests=False)


def test_verify_librepy_archive_missing_metadata(tmp_path: Path):
    zip_path = tmp_path / "missing_meta.oxt"
    _create_synthetic_zip(zip_path, omit_metadata=True)
    with pytest.raises(BuildError, match="METADATA"):
        verify_librepy_archive(str(zip_path), with_tests=False)


def test_verify_librepy_archive_tests_entry_forbidden_in_release(tmp_path: Path):
    zip_path = tmp_path / "has_test.oxt"
    _create_synthetic_zip(zip_path, extra_entries={"tests/test_foo.py": "#\n"})
    with pytest.raises(BuildError, match="test entry"):
        verify_librepy_archive(str(zip_path), with_tests=False)
    # with_tests=True skips the test check
    verify_librepy_archive(str(zip_path), with_tests=True)


def test_verify_librepy_archive_missing_manifest_entry(tmp_path: Path):
    zip_path = tmp_path / "missing_manifest_entry.oxt"
    _create_synthetic_zip(zip_path, manifest_entries=["plugin/main_core.py", "absent_file.py"])
    with pytest.raises(BuildError, match="manifest.xml entry not in archive"):
        verify_librepy_archive(str(zip_path), with_tests=False)


def test_verify_librepy_archive_wrong_description_version(tmp_path: Path):
    zip_path = tmp_path / "wrong_ver.oxt"
    _create_synthetic_zip(zip_path, description_version="0.0.1")
    with pytest.raises(BuildError, match="description.xml version"):
        verify_librepy_archive(str(zip_path), with_tests=False)


def test_librepy_manifest_cmd_and_makefile_recipe():
    cmd = librepy_manifest_cmd(str(_REPO_ROOT))
    assert cmd.count("scripting") == 1
    assert cmd.count("vision") == 1

    makefile_content = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    lines = makefile_content.splitlines()
    manifest_core_lines: list[str] = []
    in_target = False
    for line in lines:
        if line.startswith("manifest-core:"):
            in_target = True
            continue
        if in_target:
            if line.startswith(("\t", " ")):
                manifest_core_lines.append(line)
            else:
                break
    recipe = "\n".join(manifest_core_lines)
    assert "scripting" not in recipe
    assert "vision" not in recipe
    assert "--manifest-only" in recipe
def test_missing_generated_settings_dialog_fails_assembly(monkeypatch, tmp_path: Path):
    from scripts.build_librepy_oxt import BuildError, assemble_librepy_bundle

    monkeypatch.setattr("scripts.build_librepy_oxt._ensure_librepy_manifest", lambda base_dir: None)
    monkeypatch.setattr("scripts.build_librepy_oxt.patch_description_xml", lambda dest: None)

    real_isfile = os.path.isfile

    def fake_isfile(path: str) -> bool:
        if "SettingsDialog.xdl" in path:
            return False
        return real_isfile(path)

    monkeypatch.setattr("os.path.isfile", fake_isfile)
    with pytest.raises(BuildError, match="SettingsDialog.xdl"):
        assemble_librepy_bundle(str(_REPO_ROOT), bundle_dir=str(tmp_path / "bundle"))


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
