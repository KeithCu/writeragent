# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibrePy registers =PY() under the WriterAgent add-in namespace for formula portability."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CALC_ADDINS = _REPO_ROOT / "extension-core" / "registry" / "org" / "openoffice" / "Office" / "CalcAddIns.xcu"
_IDL = _REPO_ROOT / "extension-core" / "idl" / "XPythonFunction.idl"
_ADDIN_PY = _REPO_ROOT / "plugin" / "calc" / "python" / "addin_librepy.py"

_WRITERAGENT_ADDIN = "org.extension.writeragent.PythonFunction"
_LIBREPY_ADDIN = "org.extension.librepy.PythonFunction"

_OOR_NS = "http://openoffice.org/2001/registry"
_OOR_NAME = "{%s}name" % _OOR_NS


def test_librepy_calcaddins_uses_writeragent_pythonfunction_node():
    root = ET.parse(_CALC_ADDINS).getroot()
    nodes = [
        node.get(_OOR_NAME)
        for node in root.iter("node")
        if node.get(_OOR_NAME) and "PythonFunction" in (node.get(_OOR_NAME) or "")
    ]
    assert _WRITERAGENT_ADDIN in nodes
    assert _LIBREPY_ADDIN not in nodes


def test_librepy_idl_uses_writeragent_module_path():
    text = _IDL.read_text(encoding="utf-8")
    assert "module writeragent" in text
    assert "module librepy" not in text
    assert "__org_extension_writeragent_PythonFunction" in text


def test_addin_librepy_registers_writeragent_implementation():
    text = _ADDIN_PY.read_text(encoding="utf-8")
    assert f'_IMPL_NAME = "{_WRITERAGENT_ADDIN}"' in text
    assert "org.extension.writeragent.PythonFunction import" in text
    assert _LIBREPY_ADDIN not in text
