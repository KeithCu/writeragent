# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for manifest discovery, validation, and topological sorting."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from generate_manifest import find_modules, generate_manifest_py, validate_modules
from manifest_common import write_if_changed
from manifest_registry import generate_settings_dialog_tabs, patch_description_xml
from plugin.framework.errors import ConfigError
from plugin.framework.module_graph import topo_sort

_PLUGIN_DIR = _REPO_ROOT / "plugin"


def test_filters_declared_name_and_path_name():
    # Matches declared name ("core") and path-derived name ("framework")
    mods_core = [m["name"] for m in find_modules(str(_PLUGIN_DIR), ["core"])]
    mods_fw = [m["name"] for m in find_modules(str(_PLUGIN_DIR), ["framework"])]
    assert mods_core == ["core"]
    assert mods_fw == ["core"]

    # Matches declared name ("agent_backend") and path-derived name ("acp")
    mods_ab = [m["name"] for m in find_modules(str(_PLUGIN_DIR), ["agent_backend"])]
    mods_acp = [m["name"] for m in find_modules(str(_PLUGIN_DIR), ["acp"])]
    assert mods_ab == ["agent_backend"]
    assert mods_acp == ["agent_backend"]


def test_filter_core_plus_mcp_includes_core():
    mods = [m["name"] for m in find_modules(str(_PLUGIN_DIR), ["core", "mcp"])]
    assert mods == ["core", "mcp"]


def test_filter_unknown_token_fails():
    with pytest.raises(ValueError, match="Unknown module filter token: 'unknown_token'"):
        find_modules(str(_PLUGIN_DIR), ["unknown_token"])

    with pytest.raises(ValueError, match="Unknown module filter token: 'unknown_token'"):
        find_modules(str(_PLUGIN_DIR), ["core", "unknown_token"])


def test_filter_scripting_plus_vision_returns_those_two():
    mods = [m["name"] for m in find_modules(str(_PLUGIN_DIR), ["scripting", "vision"])]
    assert mods == ["scripting", "vision"]


def test_skipped_directories_ignored(tmp_path):
    # Setup dummy tree with skipped dirs and valid dir
    for skip in ("lib", "contrib", "tests", "__pycache__", ".hidden"):
        d = tmp_path / skip / "sub"
        d.mkdir(parents=True)
        (d / "module.yaml").write_text("name: should_be_skipped\n", encoding="utf-8")

    valid_dir = tmp_path / "valid"
    valid_dir.mkdir(parents=True)
    (valid_dir / "module.yaml").write_text("name: valid\n", encoding="utf-8")

    manifests = find_modules(str(tmp_path))
    names = [m["name"] for m in manifests]
    assert names == ["valid"]


def test_validation_duplicate_module_name():
    modules = [
        {"name": "foo", "_yaml_path": "path/a/module.yaml"},
        {"name": "foo", "_yaml_path": "path/b/module.yaml"},
    ]
    with pytest.raises(ValueError, match="path/b/module.yaml: name: duplicate module name 'foo'"):
        validate_modules(modules)


def test_validation_duplicate_provides_services():
    modules = [
        {"name": "a", "provides_services": ["svc_x"], "_yaml_path": "a/module.yaml"},
        {"name": "b", "provides_services": ["svc_x"], "_yaml_path": "b/module.yaml"},
    ]
    with pytest.raises(ValueError, match="b/module.yaml: provides_services: duplicate service 'svc_x'"):
        validate_modules(modules)


def test_validation_unresolved_requires():
    modules = [
        {"name": "a", "requires": ["missing_dep"], "_yaml_path": "a/module.yaml"},
    ]
    with pytest.raises(ValueError, match="a/module.yaml: requires: unresolved requirement 'missing_dep'"):
        validate_modules(modules)


def test_validation_bad_widget():
    modules = [
        {
            "name": "a",
            "config": {"field_x": {"widget": "slider_unknown", "type": "int"}},
            "_yaml_path": "a/module.yaml",
        }
    ]
    with pytest.raises(ValueError, match="a/module.yaml: config.field_x.widget: invalid widget 'slider_unknown'"):
        validate_modules(modules)


def test_validation_select_missing_options():
    modules = [
        {
            "name": "a",
            "config": {"field_s": {"widget": "select", "type": "string"}},
            "_yaml_path": "a/module.yaml",
        }
    ]
    with pytest.raises(ValueError, match="a/module.yaml: config.field_s.options: widget select requires options"):
        validate_modules(modules)


def test_validation_combo_without_options_allowed():
    modules = [
        {
            "name": "a",
            "config": {"field_c": {"widget": "combo", "type": "string"}},
            "_yaml_path": "a/module.yaml",
        }
    ]
    # Allowed: combo with runtime options
    validate_modules(modules)


def test_validation_inverted_min_max():
    modules = [
        {
            "name": "a",
            "config": {"field_n": {"widget": "number", "type": "int", "min": 10, "max": 2}},
            "_yaml_path": "a/module.yaml",
        }
    ]
    with pytest.raises(ValueError, match=r"a/module.yaml: config.field_n.min: min 10 > max 2"):
        validate_modules(modules)


@pytest.mark.parametrize(
    "stype, default_val",
    [
        ("int", True),
        ("int", "10"),
        ("float", True),
        ("float", "1.5"),
        ("bool", "true"),
        ("bool", 1),
        ("boolean", 0),
    ],
)
def test_validation_bad_default_type(stype, default_val):
    modules = [
        {
            "name": "a",
            "config": {"field_t": {"type": stype, "default": default_val}},
            "_yaml_path": "a/module.yaml",
        }
    ]
    with pytest.raises(ValueError, match=r"a/module.yaml: config.field_t.default"):
        validate_modules(modules)


def test_validation_missing_icon(tmp_path):
    modules = [
        {
            "name": "a",
            "actions": {"act": {"icon": "nonexistent_icon_123"}},
            "_yaml_path": "a/module.yaml",
        }
    ]
    with pytest.raises(ValueError, match="actions.act.icon: icon file not found"):
        validate_modules(modules, project_root=str(tmp_path))


def test_validation_config_inline_empty_or_non_string():
    modules = [
        {"name": "a", "config_inline": "", "_yaml_path": "a/module.yaml"},
    ]
    with pytest.raises(ValueError, match="a/module.yaml: config_inline: must be a non-empty string"):
        validate_modules(modules)


def test_validation_config_inline_self():
    modules = [
        {"name": "a", "config_inline": "a", "_yaml_path": "a/module.yaml"},
    ]
    with pytest.raises(ValueError, match="a/module.yaml: config_inline: cannot inline into self 'a'"):
        validate_modules(modules)


def test_validation_config_inline_unknown_target():
    modules = [
        {"name": "a", "config_inline": "missing", "_yaml_path": "a/module.yaml"},
    ]
    with pytest.raises(ValueError, match="a/module.yaml: config_inline: unknown target module 'missing'"):
        validate_modules(modules)


def test_validation_config_inline_chained_target():
    modules = [
        {"name": "a", "config_inline": "b", "_yaml_path": "a/module.yaml"},
        {"name": "b", "config_inline": "c", "_yaml_path": "b/module.yaml"},
        {"name": "c", "_yaml_path": "c/module.yaml"},
    ]
    with pytest.raises(ValueError, match="a/module.yaml: config_inline: target module 'b' cannot itself set config_inline"):
        validate_modules(modules)


def test_validation_config_dialog_not_dict():
    modules = [
        {"name": "a", "config_dialog": "invalid", "_yaml_path": "a/module.yaml"},
    ]
    with pytest.raises(ValueError, match="a/module.yaml: config_dialog: must be a dict"):
        validate_modules(modules)


def test_validation_config_dialog_invalid_id():
    modules = [
        {"name": "a", "config_dialog": {"id": "123Bad-Id"}, "_yaml_path": "a/module.yaml"},
    ]
    with pytest.raises(ValueError, match=r"a/module.yaml: config_dialog.id: invalid dialog id '123Bad-Id'"):
        validate_modules(modules)


def test_validation_config_dialog_empty_buttons():
    modules = [
        {"name": "a", "config_dialog": {"buttons": []}, "_yaml_path": "a/module.yaml"},
    ]
    with pytest.raises(ValueError, match="a/module.yaml: config_dialog.buttons: must be a non-empty list"):
        validate_modules(modules)


def test_validation_config_dialog_invalid_button():
    modules = [
        {"name": "a", "config_dialog": {"buttons": ["apply", "reset"]}, "_yaml_path": "a/module.yaml"},
    ]
    with pytest.raises(ValueError, match="a/module.yaml: config_dialog.buttons: invalid button 'reset'"):
        validate_modules(modules)


@pytest.mark.parametrize(
    "bad_key",
    ["__module__", "btn_apply", "btn_ok", "btn_close", "btn_tab_general", "hlp_tip"],
)
def test_validation_reserved_config_key_name(bad_key):
    modules = [
        {"name": "a", "config": {bad_key: {"type": "string"}}, "_yaml_path": "a/module.yaml"},
    ]
    with pytest.raises(ValueError, match=rf"a/module.yaml: config\.{bad_key}: reserved config key name '{bad_key}'"):
        validate_modules(modules)



def test_topo_sort_cycle_fails():
    modules = [
        {"name": "a", "requires": ["b"]},
        {"name": "b", "requires": ["a"]},
    ]
    with pytest.raises(ConfigError, match="Cyclic module requires graph"):
        topo_sort(modules)


def test_topo_sort_real_modules_puts_core_first_and_providers_before_users():
    manifests = find_modules(str(_PLUGIN_DIR))
    manifests.sort(key=lambda m: m["name"])
    ordered = topo_sort(manifests)
    pos = {m["name"]: i for i, m in enumerate(ordered)}
    provides = {svc: m["name"] for m in manifests for svc in m.get("provides_services", [])}
    assert ordered[0]["name"] == "core"
    assert sorted(pos) == sorted(m["name"] for m in manifests)
    for m in manifests:
        for req in m.get("requires", []):
            provider = provides.get(req, req)
            if provider in pos and provider != m["name"]:
                assert pos[provider] < pos[m["name"]], (provider, m["name"])


def test_topo_sort_independent_modules_preserves_input_order():
    pair1 = [{"name": "beta"}, {"name": "alpha"}]
    assert [m["name"] for m in topo_sort(pair1)] == ["beta", "alpha"]

    pair2 = [{"name": "alpha"}, {"name": "beta"}]
    assert [m["name"] for m in topo_sort(pair2)] == ["alpha", "beta"]


def test_generate_manifest_py_roundtrip_literals(tmp_path):
    out = tmp_path / "_manifest.py"
    modules = [
        {
            "name": "sample",
            "title": "Title with Unicode: 日本語 and quotes: \"hello\"",
            "requires": [],
            "provides_services": [],
            "config": {
                "flag": {"type": "boolean", "default": True},
                "null_val": {"type": "string", "default": None},
                "text_val": {"type": "string", "default": 'Escaped "quotes" and \'single\''},
                "unicode_val": {"type": "string", "default": "Привет мир"},
            },
            "actions": {"act": {"icon": "sample"}},
        }
    ]
    generate_manifest_py(modules, str(out))
    text = out.read_text(encoding="utf-8")
    tree = ast.parse(text)
    # Find MODULES assignment
    modules_node = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id == "MODULES":
                    modules_node = stmt.value
    assert modules_node is not None
    loaded_modules = ast.literal_eval(modules_node)
    assert len(loaded_modules) == 1
    mod = loaded_modules[0]
    assert mod["name"] == "sample"
    assert mod["title"] == 'Title with Unicode: 日本語 and quotes: "hello"'
    assert mod["config"]["flag"]["default"] is True
    assert mod["config"]["null_val"]["default"] is None
    assert mod["config"]["text_val"]["default"] == 'Escaped "quotes" and \'single\''
    assert mod["config"]["unicode_val"]["default"] == "Привет мир"


def test_generate_manifest_py_rejects_non_finite_float(tmp_path):
    out = tmp_path / "_manifest.py"
    modules = [
        {
            "name": "bad_nan",
            "config": {"val": {"type": "float", "default": float("nan")}},
        }
    ]
    with pytest.raises(ValueError, match="Non-finite float"):
        generate_manifest_py(modules, str(out))


def test_generate_manifest_py_emits_only_modules_table(tmp_path):
    out = tmp_path / "_manifest.py"
    manifests = find_modules(str(_PLUGIN_DIR))
    generate_manifest_py(manifests, str(out))
    source = out.read_text(encoding="utf-8")
    code = compile(source, str(out), "exec")
    ns: dict[str, object] = {}
    exec(code, ns)
    assert "MODULES" in ns
    assert isinstance(ns["MODULES"], list)
    assert "VERSION" not in ns
    assert "CONFIG_DEFAULTS" not in ns
    assert "CONFIG_SCHEMAS" not in ns
    assert "DOTTED_FALLBACKS" not in ns
    for mod in ns["MODULES"]:
        assert isinstance(mod, dict)
        assert "actions" not in mod
        assert "action_icons" in mod


def test_generate_settings_dialog_tabs_missing_template(tmp_path):
    with pytest.raises(FileNotFoundError, match="Template not found"):
        generate_settings_dialog_tabs([], str(tmp_path / "missing.tpl"), str(tmp_path / "out.xdl"))


def test_patch_description_xml_missing_template(tmp_path):
    with pytest.raises(FileNotFoundError, match="description.xml.tpl not found"):
        patch_description_xml(str(tmp_path))


def test_generate_settings_dialog_tabs_missing_marker(tmp_path):
    tpl = tmp_path / "bad.tpl"
    # Missing END AUTO_GENERATED_TABS
    tpl.write_text(
        '<dlg:window xmlns:dlg="http://openoffice.org/2000/dialog">\n'
        '<dlg:bulletinboard>\n'
        '<!-- AUTO_GENERATED_TABS -->\n'
        '<!-- AUTO_GENERATED_PAGES -->\n'
        '<!-- END AUTO_GENERATED_PAGES -->\n'
        '</dlg:bulletinboard>\n'
        '</dlg:window>\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Required marker '<!-- END AUTO_GENERATED_TABS -->' missing"):
        generate_settings_dialog_tabs([], str(tpl), str(tmp_path / "out.xdl"))


def test_write_if_changed_atomic(tmp_path, monkeypatch):
    import os

    target = tmp_path / "target.txt"
    target.write_text("initial", encoding="utf-8")

    def failing_replace(src, dst):
        raise OSError("Simulated disk error during atomic replace")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError, match="Simulated disk error"):
        write_if_changed(str(target), "new content")

    # Original content preserved intact
    assert target.read_text(encoding="utf-8") == "initial"
    # No temporary files left behind in the directory
    remaining_files = [p.name for p in tmp_path.iterdir()]
    assert remaining_files == ["target.txt"]


def test_generate_settings_dialog_tabs_idempotent(tmp_path):
    modules = find_modules(str(_PLUGIN_DIR))
    tpl_path = _REPO_ROOT / "extension" / "Dialogs" / "SettingsDialog.xdl.tpl"
    out1 = tmp_path / "SettingsDialog.xdl"
    out2 = tmp_path / "SettingsDialog_round2.xdl"

    generate_settings_dialog_tabs(modules, str(tpl_path), str(out1))
    content1 = out1.read_text(encoding="utf-8")

    # Second run with same input produces byte-identical output
    generate_settings_dialog_tabs(modules, str(tpl_path), str(out1))
    content2 = out1.read_text(encoding="utf-8")
    assert content1 == content2

    # Running on already-spliced output also preserves exact output
    generate_settings_dialog_tabs(modules, str(out1), str(out2))
    content3 = out2.read_text(encoding="utf-8")
    assert content1 == content3
