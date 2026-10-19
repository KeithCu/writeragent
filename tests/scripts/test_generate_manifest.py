# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for module manifest loading, validation, filtering, and XDL/manifest generation."""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from scripts.generate_manifest import filter_modules, find_modules, topo_sort, validate_manifests  # noqa: E402
from scripts.manifest_registry import generate_manifest_xml, generate_settings_dialog_tabs  # noqa: E402


def test_filter_modules_cli_options():
    manifests = find_modules(str(_REPO_ROOT / "plugin"))

    core_filtered = filter_modules(manifests, ["core"])
    assert [m["name"] for m in core_filtered] == ["core"]

    agent_backend_filtered = filter_modules(manifests, ["agent_backend"])
    assert [m["name"] for m in agent_backend_filtered] == ["agent_backend"]

    acp_filtered = filter_modules(manifests, ["acp"])
    assert [m["name"] for m in acp_filtered] == ["agent_backend"]

    mcp_filtered = filter_modules(manifests, ["mcp"])
    assert {m["name"] for m in mcp_filtered} == {"mcp", "agent_backend"}

    core_two = filter_modules(manifests, ["scripting", "vision"])
    assert [m["name"] for m in core_two] == ["scripting", "vision"]


def test_validation_catches_invalid_manifests():
    # Duplicate name
    m_dup = [
        {"name": "foo", "_yaml_path": "foo1.yaml"},
        {"name": "foo", "_yaml_path": "foo2.yaml"},
    ]
    errs = validate_manifests(m_dup)
    assert any("duplicate module name 'foo'" in e for e in errs)

    # Dependency cycle
    m_cycle = [
        {"name": "a", "requires": ["b"], "_yaml_path": "a.yaml"},
        {"name": "b", "requires": ["a"], "_yaml_path": "b.yaml"},
    ]
    errs = validate_manifests(m_cycle)
    assert any("dependency cycle" in e for e in errs)

    # Unknown widget
    m_widget = [
        {"name": "foo", "_yaml_path": "foo.yaml", "config": {"f": {"widget": "invalid_widget"}}}
    ]
    errs = validate_manifests(m_widget)
    assert any("invalid widget 'invalid_widget'" in e for e in errs)

    # select without options
    m_select = [
        {"name": "foo", "_yaml_path": "foo.yaml", "config": {"f": {"widget": "select"}}}
    ]
    errs = validate_manifests(m_select)
    assert any("select widget requires options or options_provider" in e for e in errs)

    # config_inline target that is not a module
    m_inline = [
        {"name": "foo", "config_inline": "missing_module", "_yaml_path": "foo.yaml"}
    ]
    errs = validate_manifests(m_inline)
    assert any("target module 'missing_module' not found" in e for e in errs)

    # combo with no options passes
    m_combo = [
        {"name": "foo", "_yaml_path": "foo.yaml", "config": {"f": {"widget": "combo"}}}
    ]
    errs = validate_manifests(m_combo)
    assert not errs


def test_topo_sort_matches_module_loader():
    from plugin.framework.module_base import ModuleLoader

    manifests = find_modules(str(_REPO_ROOT / "plugin"))

    build_order = [m["name"] for m in topo_sort(manifests)]
    loader_order = [m["name"] for m in ModuleLoader.topo_sort(manifests)]
    assert build_order == loader_order


def test_template_missing_markers_raises(tmp_path):
    tpl_file = tmp_path / "SettingsDialog.xdl.tpl"
    out_file = tmp_path / "SettingsDialog.xdl"

    tpl_file.write_text("<x><!-- AUTO_GENERATED_TABS --></x>", encoding="utf-8")
    with pytest.raises(ValueError, match="missing auto-generated markers"):
        generate_settings_dialog_tabs([], str(tpl_file), str(out_file))

    tpl_file.write_text("<x><!-- AUTO_GENERATED_TABS --><!-- AUTO_GENERATED_PAGES --></x>", encoding="utf-8")
    with pytest.raises(ValueError, match="missing anchor comments"):
        generate_settings_dialog_tabs([], str(tpl_file), str(out_file))

    # Real template gives Speech/Python controls dlg:page >= 3, and btn_ok / btn_cancel remain
    real_tpl = _REPO_ROOT / "extension" / "Dialogs" / "SettingsDialog.xdl.tpl"
    manifests = find_modules(str(_REPO_ROOT / "plugin"))
    generate_settings_dialog_tabs(manifests, str(real_tpl), str(out_file))
    content = out_file.read_text(encoding="utf-8")
    assert 'dlg:id="btn_ok"' in content
    assert 'dlg:id="btn_cancel"' in content
    assert 'dlg:id="scripting__python_venv_path"' in content

    root = ET.fromstring(content)
    venv_el = None
    for el in root.iter():
        if el.attrib.get("{http://openoffice.org/2000/dialog}id") == "scripting__python_venv_path":
            venv_el = el
            break
    assert venv_el is not None
    assert int(venv_el.attrib.get("{http://openoffice.org/2000/dialog}page", "0")) >= 3


def test_config_inline_children_input_order():
    from plugin.chatbot.settings_tab_order import _build_inline_maps

    modules = [
        {"name": "parent"},
        {"name": "child1", "config_inline": "parent", "config": {"k1": {"default": 1}}},
        {"name": "child2", "config_inline": "parent", "config": {"k2": {"default": 2}}},
    ]
    _inline_set, inline_map = _build_inline_maps(modules)
    children = inline_map.get("parent", [])
    child_names = [c[0]["name"] for c in children]
    assert child_names == ["child1", "child2"]


def test_real_modules_settings_controls(tmp_path):
    real_tpl = _REPO_ROOT / "extension" / "Dialogs" / "SettingsDialog.xdl.tpl"
    out_file = tmp_path / "SettingsDialog.xdl"
    manifests = find_modules(str(_REPO_ROOT / "plugin"))
    generate_settings_dialog_tabs(manifests, str(real_tpl), str(out_file))
    content = out_file.read_text(encoding="utf-8")
    root = ET.fromstring(content)

    dlg_ns = "http://openoffice.org/2000/dialog"
    dlg_id = f"{{{dlg_ns}}}id"
    ids = []
    for el in root.iter():
        if dlg_id in el.attrib:
            ids.append(el.attrib[dlg_id])

    # No duplicate ids
    assert len(ids) == len(set(ids))

    # Internal skipped (core's log_level is internal, so absent)
    assert "log_level" not in ids
    assert "core__log_level" not in ids

    # settings_persist: false buttons kept (test_venv in scripting)
    assert "scripting__test_venv" in ids

    # Inline children prefixed
    assert "calc__ods_cache_enabled" in ids
    assert "agent_backend__backend_id" in ids

    # Inline children do not get btn_tab_<child>
    assert "btn_tab_calc" not in ids
    assert "btn_tab_agent_backend" not in ids


def test_generate_manifest_xml_paths_exist(tmp_path):
    out_file = tmp_path / "manifest.xml"
    generate_manifest_xml(str(out_file))
    root = ET.parse(out_file).getroot()
    mf_ns = "http://openoffice.org/2001/manifest"
    full_path_attr = f"{{{mf_ns}}}full-path"

    for entry in root.findall(f"{{{mf_ns}}}file-entry"):
        fp = entry.get(full_path_attr)
        if not fp:
            continue
        ext_path = _REPO_ROOT / "extension" / fp
        repo_path = _REPO_ROOT / fp
        assert ext_path.exists() or repo_path.exists(), f"manifest entry {fp} does not exist"


def test_dialog_xdl_files_exist():
    plugin_dir = _REPO_ROOT / "plugin"
    dialogs_dir = _REPO_ROOT / "extension" / "Dialogs"
    generated_names = {"SettingsDialog", "VisionSettingsDialog"}

    load_dialog_re = re.compile(r'load_writeragent_dialog(?:_detail)?\(\s*["\']([^"\']+)["\']')
    xdl_path_re = re.compile(r'XDL_PATH\s*=\s*["\']Dialogs/([^"\']+)\.xdl["\']')

    referenced_dialogs = set()
    for py_file in plugin_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for match in load_dialog_re.finditer(text):
            referenced_dialogs.add(match.group(1))
        for match in xdl_path_re.finditer(text):
            referenced_dialogs.add(match.group(1))

    for dlg_name in referenced_dialogs:
        if dlg_name in generated_names:
            continue
        xdl_file = dialogs_dir / f"{dlg_name}.xdl"
        assert xdl_file.is_file(), f"Dialog XDL not found: {xdl_file}"
