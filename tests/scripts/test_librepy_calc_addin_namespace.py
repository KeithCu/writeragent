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


def test_xpythonfunction_idl_files_identical_and_declare_caller():
    wa_idl = (_REPO_ROOT / "extension" / "idl" / "XPythonFunction.idl").read_text(encoding="utf-8")
    lp_idl = (_REPO_ROOT / "extension-core" / "idl" / "XPythonFunction.idl").read_text(encoding="utf-8")
    assert wa_idl == lp_idl
    assert "#include <com/sun/star/beans/XPropertySet.idl>" in wa_idl
    assert "any python( [in] com::sun::star::beans::XPropertySet caller, [in] string code, [in] sequence< any > data );" in wa_idl
    assert "any py( [in] com::sun::star::beans::XPropertySet caller, [in] string code, [in] sequence< any > data );" in wa_idl
