# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural tests for Calc and Writer accelerators in both OXTs."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

_OOR_NS = "http://openoffice.org/2001/registry"
_OOR_NAME = "{%s}name" % _OOR_NS

_REPO_ROOT = Path(__file__).resolve().parents[2]

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


def test_accelerators_xcu_well_formed_and_unique_keys():
    assert _commands_by_key(_LIBREPY_XCU, _CALC_SVC)
    assert _commands_by_key(_WRITERAGENT_XCU, _WRITER_SVC)
    assert _commands_by_key(_WRITERAGENT_XCU, _CALC_SVC)
