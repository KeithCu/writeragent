# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural test for registry namespace helpers and update XML generation."""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from scripts.manifest_xdl import _OOR_NS, _oor  # noqa: E402  (needs scripts on sys.path)

_OOR_NAME = "{%s}name" % _OOR_NS


def test_oor_returns_qualified_name_not_none():
    assert _oor("name") == "{%s}name" % _OOR_NS
    assert _oor("op") == "{%s}op" % _OOR_NS


def test_oor_attribute_serializes_with_oor_prefix_not_none():
    el = ET.Element("node", {_oor("name"): "X", _oor("op"): "replace"})
    xml = ET.tostring(el, encoding="unicode")
    assert 'oor:name="X"' in xml and 'oor:op="replace"' in xml
    assert "None=" not in xml


def test_generate_update_xml_default_only_generates_writeragent(tmp_path):
    """Default generate_update_xml must generate update.xml only, not update-librepy or update-libreharper."""
    from scripts.manifest_registry import generate_update_xml

    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "update.xml.tpl").write_text("<feed>{{VERSION}}</feed>", encoding="utf-8")
    (plugin_dir / "update-librepy.xml.tpl").write_text("<feed>{{VERSION}}</feed>", encoding="utf-8")
    (plugin_dir / "update-libreharper.xml.tpl").write_text("<feed>{{VERSION}}</feed>", encoding="utf-8")

    generate_update_xml(str(tmp_path))

    assert (tmp_path / "update.xml").exists()
    assert not (tmp_path / "update-librepy.xml").exists()
    assert not (tmp_path / "update-libreharper.xml").exists()


def test_generate_update_xml_specific_targets(tmp_path):
    """generate_update_xml with specific targets generates only the requested feed."""
    from scripts.manifest_registry import generate_update_xml

    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "update.xml.tpl").write_text("<feed>{{VERSION}}</feed>", encoding="utf-8")
    (plugin_dir / "update-librepy.xml.tpl").write_text("<feed>{{VERSION}}</feed>", encoding="utf-8")
    (plugin_dir / "update-libreharper.xml.tpl").write_text("<feed>{{VERSION}}</feed>", encoding="utf-8")

    generate_update_xml(str(tmp_path), "update-librepy.xml")
    assert (tmp_path / "update-librepy.xml").exists()
    assert not (tmp_path / "update.xml").exists()
    assert not (tmp_path / "update-libreharper.xml").exists()

    generate_update_xml(str(tmp_path), "update-libreharper.xml")
    assert (tmp_path / "update-libreharper.xml").exists()
