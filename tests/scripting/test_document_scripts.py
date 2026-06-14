# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for document-attached Run Python Script storage."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.document_scripts import (
    ANALYSIS_SCRIPT_DISPLAY_PREFIX,
    DOCUMENT_SCRIPTS_UDPROP,
    SCRIPT_ORIGIN_ANALYSIS,
    SCRIPT_ORIGIN_VISION,
    VISION_SCRIPT_DISPLAY_PREFIX,
    _MAX_DOCUMENT_SCRIPTS_BYTES,
    attach_document_script,
    build_scripts_list_message,
    build_xdl_script_picker_state,
    delete_document_script,
    document_script_display_name,
    get_document_scripts,
    has_document_scripts,
    parse_analysis_script_display_name,
    parse_document_script_display_name,
    parse_vision_script_display_name,
    resolve_script_picker_entry,
    set_document_scripts,
)
from plugin.tests.testing_utils import setup_uno_mocks
from tests.writer.test_document_helpers import _DocWithUserDefinedProperties, _UserDefinedProperties

setup_uno_mocks()


def test_get_document_scripts_empty():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    assert get_document_scripts(doc) == {}
    assert not has_document_scripts(doc)


def test_roundtrip_envelope():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    scripts = {"Clean Data": "result = 1", "Monte Carlo": "import random\nresult = 1"}
    assert set_document_scripts(doc, scripts) is None
    raw = props.getPropertyValue(DOCUMENT_SCRIPTS_UDPROP)
    parsed = json.loads(raw)
    assert parsed["version"] == 1
    assert parsed["scripts"] == scripts
    assert get_document_scripts(doc) == scripts
    assert has_document_scripts(doc)


def test_oversize_payload_rejected():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    big = "x" * (_MAX_DOCUMENT_SCRIPTS_BYTES + 1)
    err = set_document_scripts(doc, {"Huge": big})
    assert err is not None
    assert DOCUMENT_SCRIPTS_UDPROP not in props.values


def test_corrupt_json_returns_empty():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    props.values[DOCUMENT_SCRIPTS_UDPROP] = "not-json"
    assert get_document_scripts(doc) == {}


def test_corrupt_envelope_version_returns_empty():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    props.values[DOCUMENT_SCRIPTS_UDPROP] = json.dumps({"version": 99, "scripts": {"a": "b"}})
    assert get_document_scripts(doc) == {}


def test_attach_without_overwrite_errors_on_collision():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    assert attach_document_script(doc, "A", "code1") is None
    err = attach_document_script(doc, "A", "code2", overwrite=False)
    assert err is not None
    assert get_document_scripts(doc)["A"] == "code1"


def test_delete_document_script():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    attach_document_script(doc, "A", "code")
    assert delete_document_script(doc, "A") is None
    assert get_document_scripts(doc) == {}


def test_readonly_document_returns_error():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.isReadonly = MagicMock(return_value=True)
    err = set_document_scripts(doc, {"A": "x"})
    assert err is not None
    assert DOCUMENT_SCRIPTS_UDPROP not in props.values


def test_display_name_helpers():
    assert document_script_display_name("Foo") == "[Doc] Foo"
    assert parse_document_script_display_name("[Doc] Foo") == "Foo"
    assert parse_document_script_display_name("Foo") is None


def test_build_xdl_script_picker_state():
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    attach_document_script(doc, "DocScript", "result = 2")
    items, merged, origin_map = build_xdl_script_picker_state(
        ctx,
        doc,
        {"UserScript": "result = 1"},
    )
    assert "Sample" not in items
    assert "UserScript" in items
    assert "[Doc] DocScript" in items
    assert merged["UserScript"] == "result = 1"
    assert merged["[Doc] DocScript"] == "result = 2"
    assert origin_map["UserScript"] == "user"
    assert origin_map["[Doc] DocScript"] == "document"


def test_resolve_script_picker_entry():
    origin_map = {"Mine": "user", "[Doc] Shared": "document"}
    assert resolve_script_picker_entry("Mine", origin_map) == ("Mine", "user")
    assert resolve_script_picker_entry("[Doc] Shared", origin_map) == ("Shared", "document")


def test_build_scripts_list_message_sections():
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/test.odt")
    attach_document_script(doc, "Regional", "result = 3")
    with patch("plugin.framework.config.get_config", return_value={"Prime": "result = 2"}), patch(
        "plugin.framework.config.get_config_str", return_value=""
    ), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key", return_value="last_python_script_name_writer"
    ):
        msg = build_scripts_list_message(ctx, session_doc=doc, session_doc_url="file:///tmp/test.odt")
    assert msg["document_available"] is True
    assert msg["document_stale"] is False
    sections = {s["id"]: s["scripts"] for s in msg["sections"]}
    assert sections["user"] == {"Prime": "result = 2"}
    assert sections["document"] == {"Regional": "result = 3"}


def test_build_scripts_list_message_includes_sample_code():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={"Prime": "print('scratchpad')"}), patch(
        "plugin.framework.config.get_config_str", return_value="Prime"
    ) as mock_get_str, patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key", return_value="last_python_script_name_writer"
    ) as mock_key:
        msg = build_scripts_list_message(ctx, session_doc=doc, session_doc_url=None)
    mock_key.assert_called_once_with(doc)
    mock_get_str.assert_called_once_with(ctx, "last_python_script_name_writer")
    assert msg["sample_code"] == "print('scratchpad')"


def test_build_scripts_list_message_stale_when_url_changes():
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/other.odt")
    attach_document_script(doc, "A", "x")
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.framework.config.get_config_str", return_value=""
    ), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key", return_value="last_python_script_name_writer"
    ):
        msg = build_scripts_list_message(ctx, session_doc=doc, session_doc_url="file:///tmp/original.odt")
    assert msg["document_stale"] is True
    sections = {s["id"]: s["scripts"] for s in msg["sections"]}
    assert sections["document"] == {}


def test_build_scripts_list_includes_analysis_section_for_calc():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.scripting.document_scripts.is_calc", return_value=True
    ):
        msg = build_scripts_list_message(ctx, session_doc=doc, session_doc_url=None)
    section_ids = [s["id"] for s in msg["sections"]]
    assert SCRIPT_ORIGIN_ANALYSIS in section_ids
    analysis = next(s for s in msg["sections"] if s["id"] == SCRIPT_ORIGIN_ANALYSIS)
    assert f"{ANALYSIS_SCRIPT_DISPLAY_PREFIX}describe_data" in analysis["scripts"]


def test_resolve_analysis_script_picker_entry():
    display = f"{ANALYSIS_SCRIPT_DISPLAY_PREFIX}describe_data"
    origin_map = {display: SCRIPT_ORIGIN_ANALYSIS}
    assert resolve_script_picker_entry(display, origin_map) == ("describe_data", SCRIPT_ORIGIN_ANALYSIS)
    assert parse_analysis_script_display_name(display) == "describe_data"


def test_build_scripts_list_includes_vision_section_for_writer():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.vision.vision_runner.supports_vision_manual", return_value=True
    ):
        msg = build_scripts_list_message(ctx, session_doc=doc, session_doc_url=None)
    section_ids = [s["id"] for s in msg["sections"]]
    assert SCRIPT_ORIGIN_VISION in section_ids
    vision = next(s for s in msg["sections"] if s["id"] == SCRIPT_ORIGIN_VISION)
    assert f"{VISION_SCRIPT_DISPLAY_PREFIX}extract_text" in vision["scripts"]
    assert f"{VISION_SCRIPT_DISPLAY_PREFIX}extract_structure" in vision["scripts"]


def test_build_scripts_list_includes_vision_section_for_calc():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.scripting.document_scripts.is_calc", return_value=True
    ), patch("plugin.scripting.document_scripts.is_writer", return_value=False), patch(
        "plugin.vision.vision_runner.supports_vision_manual", return_value=True
    ):
        msg = build_scripts_list_message(ctx, session_doc=doc, session_doc_url=None)
    section_ids = [s["id"] for s in msg["sections"]]
    assert SCRIPT_ORIGIN_VISION in section_ids
    vision = next(s for s in msg["sections"] if s["id"] == SCRIPT_ORIGIN_VISION)
    assert f"{VISION_SCRIPT_DISPLAY_PREFIX}extract_text" in vision["scripts"]


def test_build_scripts_list_excludes_vision_section_for_draw():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.scripting.document_scripts.is_draw", return_value=True
    ), patch("plugin.scripting.document_scripts.is_calc", return_value=False), patch(
        "plugin.scripting.document_scripts.is_writer", return_value=False
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=False):
        msg = build_scripts_list_message(ctx, session_doc=doc, session_doc_url=None)
    section_ids = [s["id"] for s in msg["sections"]]
    assert SCRIPT_ORIGIN_VISION not in section_ids


def test_build_xdl_script_picker_includes_vision_for_writer():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        items, merged, origin_map = build_xdl_script_picker_state(ctx, doc, {})
    for helper in ("extract_text", "extract_structure"):
        display = f"{VISION_SCRIPT_DISPLAY_PREFIX}{helper}"
        assert display in items
        assert display in merged
        assert origin_map[display] == SCRIPT_ORIGIN_VISION


def test_resolve_vision_script_picker_entry():
    display = f"{VISION_SCRIPT_DISPLAY_PREFIX}extract_text"
    origin_map = {display: SCRIPT_ORIGIN_VISION}
    assert resolve_script_picker_entry(display, origin_map) == ("extract_text", SCRIPT_ORIGIN_VISION)
    assert parse_vision_script_display_name(display) == "extract_text"
