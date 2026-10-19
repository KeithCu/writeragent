# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural tests for Calc and Writer accelerators in both OXTs."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_OOR_NS = "http://openoffice.org/2001/registry"
_OOR_NAME = "{%s}name" % _OOR_NS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_CALC_SVC = "com.sun.star.sheet.SpreadsheetDocument"
_WRITER_SVC = "com.sun.star.text.TextDocument"
_LIBREPY_XCU = _REPO_ROOT / "extension-core" / "Accelerators.xcu"
_WRITERAGENT_XCU = _REPO_ROOT / "extension" / "Accelerators.xcu"


def _commands_by_key(xcu: Path, module_svc: str) -> dict[str, str]:
    root = ET.parse(xcu).getroot()
    out: dict[str, str] = {}
    for modules in root.iter("node"):
        if modules.get(_OOR_NAME) != "Modules":
            continue
        for svc in modules.findall("node"):
            if svc.get(_OOR_NAME) != module_svc:
                continue
            for key_node in svc.findall("node"):
                key = key_node.get(_OOR_NAME)
                if not key:
                    continue
                if key in out:
                    raise AssertionError(f"Duplicate key {key} under {module_svc}")
                for prop in key_node.findall("prop"):
                    if prop.get(_OOR_NAME) == "Command":
                        value = prop.find("value")
                        if value is not None and value.text:
                            out[key] = value.text.strip()
    return out


def test_librepy_accelerators_use_librepy_protocol():
    cmds = _commands_by_key(_LIBREPY_XCU, _CALC_SVC)
    assert cmds["F9_SHIFT_MOD1_MOD2"] == "org.extension.librepy:scripting.reset_python_session"
    assert cmds["P_SHIFT_MOD1_MOD2"] == "org.extension.librepy:scripting.edit_python_cell"


def test_librepy_manifest_registers_accelerators():
    text = (_REPO_ROOT / "extension-core" / "META-INF" / "manifest.xml").read_text(encoding="utf-8")
    assert 'manifest:full-path="Accelerators.xcu"' in text


def test_writeragent_accelerators_keep_chat_and_add_python():
    writer_cmds = _commands_by_key(_WRITERAGENT_XCU, _WRITER_SVC)
    assert writer_cmds["Q_MOD1"] == "service:org.extension.writeragent.Main?ExtendSelection"
    assert writer_cmds["E_MOD1"] == "service:org.extension.writeragent.Main?EditSelection"

    calc_cmds = _commands_by_key(_WRITERAGENT_XCU, _CALC_SVC)
    assert calc_cmds["Q_MOD1"] == "service:org.extension.writeragent.Main?ExtendSelection"
    assert calc_cmds["E_MOD1"] == "service:org.extension.writeragent.Main?EditSelection"
    assert calc_cmds["F9_SHIFT_MOD1_MOD2"] == "org.extension.writeragent:scripting.reset_python_session"
    assert calc_cmds["P_SHIFT_MOD1_MOD2"] == "org.extension.writeragent:scripting.edit_python_cell"


def test_commands_by_key_raises_on_duplicate_key(tmp_path):
    dup_xcu = tmp_path / "dup.xcu"
    dup_xcu.write_text(
        '<oor:component-data xmlns:oor="http://openoffice.org/2001/registry" oor:name="Accelerators">'
        '  <node oor:name="PrimaryKeys">'
        '    <node oor:name="Modules">'
        '      <node oor:name="com.sun.star.text.TextDocument">'
        '        <node oor:name="Q_MOD1"><prop oor:name="Command"><value>cmd1</value></prop></node>'
        '        <node oor:name="Q_MOD1"><prop oor:name="Command"><value>cmd2</value></prop></node>'
        '      </node>'
        '    </node>'
        '  </node>'
        '</oor:component-data>',
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="Duplicate key Q_MOD1"):
        _commands_by_key(dup_xcu, _WRITER_SVC)


def test_no_module_yaml_has_top_level_menus_or_shortcuts():
    import yaml

    plugin_dir = _REPO_ROOT / "plugin"
    for path in plugin_dir.rglob("module.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert "menus" not in data, f"{path} has top-level 'menus:'"
        assert "shortcuts" not in data, f"{path} has top-level 'shortcuts:'"
