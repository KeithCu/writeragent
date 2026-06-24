# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural test for generated registry XCU (E3).

Regression guard: `_oor` once returned None, so ElementTree emitted ``<node None="...">`` and every
generated Addons.xcu was structurally invalid -- undetected because nothing parsed the generator's
output. This pins that the qualified-name helper is sane and that a generated Addons.xcu is
well-formed with proper oor:name attributes (no None=).
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from manifest_xdl import _OOR_NS, _oor  # noqa: E402  (needs scripts on sys.path)

_OOR_NAME = "{%s}name" % _OOR_NS


def test_oor_returns_qualified_name_not_none():
    assert _oor("name") == "{%s}name" % _OOR_NS
    assert _oor("op") == "{%s}op" % _OOR_NS


def test_oor_attribute_serializes_with_oor_prefix_not_none():
    el = ET.Element("node", {_oor("name"): "X", _oor("op"): "replace"})
    xml = ET.tostring(el, encoding="unicode")
    assert 'oor:name="X"' in xml and 'oor:op="replace"' in xml
    assert "None=" not in xml


def test_generated_addons_xcu_is_well_formed_with_oor_names(tmp_path):
    from manifest_registry import generate_addons_xcu

    out = tmp_path / "Addons.xcu"
    generate_addons_xcu([], {}, str(out))
    content = out.read_text(encoding="utf-8")
    assert "None=" not in content                      # the _oor=None regression signature
    root = ET.fromstring(content)                       # must be well-formed XML
    nodes = [e for e in root.iter("node")]
    assert nodes, "expected at least the root Addons structure nodes"
    for n in nodes:
        assert _OOR_NAME in n.attrib, "a <node> is missing oor:name: %r" % (n.attrib,)
