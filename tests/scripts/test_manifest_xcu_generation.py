# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural tests for registry namespace helpers, standalone dialogs, and update XML.

Tip: `_oor` once returned None, so ElementTree emitted ``<node None="...">``; keep the helper test.
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from manifest_xdl import (  # noqa: E402  (needs scripts on sys.path)
    _OOR_NS,
    _oor,
    generate_standalone_config_dialogs,
    remove_stale_generated_xdls,
)

_OOR_NAME = "{%s}name" % _OOR_NS


def test_oor_returns_qualified_name_not_none():
    assert _oor("name") == "{%s}name" % _OOR_NS
    assert _oor("op") == "{%s}op" % _OOR_NS


def test_oor_attribute_serializes_with_oor_prefix_not_none():
    el = ET.Element("node", {_oor("name"): "X", _oor("op"): "replace"})
    xml = ET.tostring(el, encoding="unicode")
    assert 'oor:name="X"' in xml and 'oor:op="replace"' in xml
    assert "None=" not in xml


def test_generate_standalone_config_dialogs_preserves_siblings(tmp_path):
    """generate_standalone_config_dialogs writes its dialog and leaves sibling XDLs untouched."""
    dialogs_dir = tmp_path / "Dialogs"
    dialogs_dir.mkdir()
    (dialogs_dir / "SettingsDialog.xdl").write_text("<x/>", encoding="utf-8")
    (dialogs_dir / "old_gone.xdl").write_text("<x/>", encoding="utf-8")
    modules = [
        {"name": "vision", "config_dialog": {"id": "VisionSettingsDialog"}, "config": {}},
    ]
    ids = generate_standalone_config_dialogs(modules, str(tmp_path))
    assert ids == ["VisionSettingsDialog"]
    names = {p.name for p in dialogs_dir.glob("*.xdl")}
    assert "VisionSettingsDialog.xdl" in names
    assert "SettingsDialog.xdl" in names
    assert "old_gone.xdl" in names


def test_remove_stale_generated_xdls(tmp_path):
    """remove_stale_generated_xdls removes unlisted XDLs and keeps listed ones."""
    dialogs_dir = tmp_path / "Dialogs"
    dialogs_dir.mkdir()
    (dialogs_dir / "SettingsDialog.xdl").write_text("<x/>", encoding="utf-8")
    (dialogs_dir / "VisionSettingsDialog.xdl").write_text("<x/>", encoding="utf-8")
    (dialogs_dir / "chatbot.xdl").write_text("<x/>", encoding="utf-8")
    (dialogs_dir / "audio.xdl").write_text("<x/>", encoding="utf-8")
    keep = {"SettingsDialog.xdl", "VisionSettingsDialog.xdl"}
    count = remove_stale_generated_xdls(str(dialogs_dir), keep)
    assert count == 2
    names = {p.name for p in dialogs_dir.glob("*.xdl")}
    assert names == {"SettingsDialog.xdl", "VisionSettingsDialog.xdl"}


def test_generate_update_xml_default_only_generates_writeragent(tmp_path):
    """Default generate_update_xml must generate update.xml only, not update-librepy or update-libreharper."""
    from manifest_registry import generate_update_xml

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
    from manifest_registry import generate_update_xml

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
