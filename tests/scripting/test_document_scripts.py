# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for document-attached Run Python Script storage."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch


from plugin.scripting.document_scripts import (
    DOCUMENT_SCRIPTS_UDPROP,
    SCRIPT_PICKER_MESSAGE_TYPES,
    _MAX_DOCUMENT_SCRIPTS_BYTES,
    DocumentScriptError,
    DocumentScriptErrorCode,
    attach_document_script,
    build_scripts_list_message,
    build_xdl_script_picker_state,
    delete_document_script,
    delete_user_script,
    document_script_display_name,
    document_scripts_identity,
    document_scripts_write_is_stale,
    get_calc_document_from_ctx,
    get_calc_init_script,
    get_document_scripts,
    handle_editor_script_message,
    parse_document_script_display_name,
    resolve_run_script_selection,
    resolve_script_picker_entry,
    save_selected_script,
    save_user_script,
    set_calc_init_script,
    set_document_scripts,
)
from plugin.scripting.domain_registry import (
    ANALYSIS_SCRIPT_DISPLAY_PREFIX,
    DOC_SCRIPT_DISPLAY_PREFIX,
    SCRIPT_ORIGIN_ANALYSIS,
    SCRIPT_ORIGIN_VISION,
    VISION_SCRIPT_DISPLAY_PREFIX,
    parse_picker_display_name,
)
from tests.writer.test_document_helpers import _DocWithUserDefinedProperties, _UserDefinedProperties


def test_document_scripts_identity_uses_shared_trailing_slash_normalize():
    doc = MagicMock()
    doc.getURL.return_value = "file:///tmp/doc.odt/"
    assert document_scripts_identity(doc) == "file:///tmp/doc.odt"
    doc.getURL.return_value = ""
    assert document_scripts_identity(doc) == ""


def test_untitled_save_is_not_a_stale_document_script_write():
    doc = MagicMock()
    doc.getURL.return_value = "file:///tmp/saved.ods"
    assert document_scripts_write_is_stale(doc, "") is False
    assert document_scripts_write_is_stale(doc, "file:///tmp/other.ods") is True


def _calc_component_enum(*models):
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True] * len(models) + [False]
    enum.nextElement.side_effect = list(models)
    comps = MagicMock()
    comps.createEnumeration.return_value = enum
    return comps


def test_get_calc_document_from_ctx_does_not_fall_back_from_writer():
    writer = MagicMock()
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = writer
    with (
        patch("plugin.scripting.document_scripts.get_desktop", return_value=desktop),
        patch("plugin.scripting.document_scripts.is_writer", return_value=True),
        patch("plugin.scripting.document_scripts.is_draw", return_value=False),
        patch("plugin.scripting.document_scripts.is_calc", return_value=False),
    ):
        assert get_calc_document_from_ctx(MagicMock()) is None
    desktop.getComponents.assert_not_called()


def test_get_calc_document_from_ctx_returns_only_enumerated_workbook():
    calc = MagicMock()
    calc.getURL.return_value = "file:///only.ods"
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = None
    desktop.getComponents.return_value = _calc_component_enum(calc)
    with (
        patch("plugin.scripting.document_scripts.get_desktop", return_value=desktop),
        patch("plugin.scripting.document_scripts.is_calc", side_effect=lambda model: model is calc),
    ):
        assert get_calc_document_from_ctx(MagicMock()) is calc


def test_get_calc_document_from_ctx_ambiguous_enumeration_returns_none():
    first = MagicMock()
    first.getURL.return_value = "file:///a.ods"
    second = MagicMock()
    second.getURL.return_value = "file:///b.ods"
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = None
    desktop.getComponents.return_value = _calc_component_enum(first, second)
    with (
        patch("plugin.scripting.document_scripts.get_desktop", return_value=desktop),
        patch("plugin.scripting.document_scripts.is_calc", side_effect=lambda model: model in (first, second)),
    ):
        assert get_calc_document_from_ctx(MagicMock()) is None


def test_get_document_scripts_empty():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    assert get_document_scripts(doc) == {}
    assert not bool(get_document_scripts(doc))


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
    assert bool(get_document_scripts(doc))


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


def test_attach_rejects_workbook_init_name():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    err = attach_document_script(doc, "INIT", "result = 1")
    assert err is not None and "INIT" in err
    assert get_document_scripts(doc) == {}
    err_init = attach_document_script(doc, "Init", "result = 1")
    assert err_init is not None
    assert get_document_scripts(doc) == {}


def test_delete_document_script():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    attach_document_script(doc, "A", "code")
    assert delete_document_script(doc, "A") is None
    assert get_document_scripts(doc) == {}


def test_delete_document_script_rejects_workbook_init():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    assert set_document_scripts(doc, {"INIT": "result = 1", "Init": "other", "Hello": "y = 1"}) is None
    err = delete_document_script(doc, "INIT")
    assert err is not None and "INIT" in err
    err_init = delete_document_script(doc, "Init")
    assert err_init is not None and "Init" in err_init
    stored = get_document_scripts(doc)
    assert stored["INIT"] == "result = 1"
    assert stored["Init"] == "other"
    assert stored["Hello"] == "y = 1"


def test_missing_property_bag_is_not_a_successful_save():
    """No UserDefinedProperties bag used to return None without storing anything."""

    class _DocProps:
        UserDefinedProperties = None

    class _Doc:
        def getDocumentProperties(self):
            return _DocProps()

        def isReadonly(self):
            return False

    doc = _Doc()
    err = set_document_scripts(doc, {"A": "x"})
    assert err is not None
    assert get_document_scripts(doc) == {}


def test_readonly_document_returns_error():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.isReadonly = MagicMock(return_value=True)
    err = set_document_scripts(doc, {"A": "x"})
    assert err is not None
    assert DOCUMENT_SCRIPTS_UDPROP not in props.values
    assert "personal library" not in err.lower()
    assert "my scripts" not in err.lower()


def test_display_name_helpers():
    assert document_script_display_name("Foo") == "[Doc] Foo"
    assert parse_document_script_display_name("[Doc] Foo") == "Foo"
    assert parse_document_script_display_name("Foo") is None


def test_build_xdl_script_picker_state():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    attach_document_script(doc, "DocScript", "result = 2")
    with patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        items, merged, origin_map = build_xdl_script_picker_state(
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
    # Local (user) first, then document-scoped, then helper domain items
    user_idx = items.index("UserScript")
    doc_idx = items.index("[Doc] DocScript")
    assert user_idx < doc_idx
    vision_items = [item for item in items if item.startswith("[Vision] ")]
    if vision_items:
        assert doc_idx < items.index(vision_items[0])


def test_resolve_run_script_selection_uses_config_default():
    ctx = MagicMock()
    doc = MagicMock()
    saved = {
        "Hello WriterAgent": "result = 'hello'",
        "Prime Numbers": "result = 'gaps'",
    }
    with patch("plugin.framework.config.get_config_str", return_value="Prime Numbers"), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        name, code, merged = resolve_run_script_selection(ctx, doc, saved)
    assert name == "Prime Numbers"
    assert code == "result = 'gaps'"
    assert merged["Prime Numbers"] == "result = 'gaps'"


def test_resolve_run_script_selection_falls_back_to_first_name():
    ctx = MagicMock()
    doc = MagicMock()
    saved = {"Alpha": "a = 1", "Beta": "b = 2"}
    with patch("plugin.framework.config.get_config_str", return_value="Missing"), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ), patch("plugin.framework.config.set_config") as mock_set:
        name, code, merged = resolve_run_script_selection(ctx, doc, saved)
    assert name == "Alpha"
    assert code == "a = 1"
    mock_set.assert_called_once_with("last_python_script_name_writer", "Alpha")


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
    assert sections["document"] == {"[Doc] Regional": "result = 3"}
    section_ids = [s["id"] for s in msg["sections"]]
    assert section_ids[0] == "user"
    assert section_ids[1] == "document"


def test_build_scripts_list_section_order_local_doc_vision_math_units_analysis():
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/test.ods")
    doc.supportsService = MagicMock(side_effect=lambda s: s == "com.sun.star.sheet.SpreadsheetDocument")
    attach_document_script(doc, "SheetHelper", "result = 1")
    with patch("plugin.framework.config.get_config", return_value={"MyLocal": "result = 0"}), patch(
        "plugin.framework.config.get_config_str", return_value=""
    ), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key", return_value="last_python_script_name_calc"
    ), patch(
        "plugin.vision.vision_runner.supports_vision_manual", return_value=True
    ):
        msg = build_scripts_list_message(ctx, session_doc=doc, session_doc_url="file:///tmp/test.ods")
    section_ids = [s["id"] for s in msg["sections"]]
    # Must be local ("user"), then document ("document"), then vision, math, units, analysis, and the rest
    assert section_ids[:6] == ["user", "document", "vision", "math", "units", "analysis"]


def test_build_scripts_list_message_includes_selected_script():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={"Prime": "print('primes')"}), patch(
        "plugin.framework.config.get_config_str", return_value="Prime"
    ) as mock_get_str, patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key", return_value="last_python_script_name_writer"
    ) as mock_key:
        msg = build_scripts_list_message(ctx, session_doc=doc, session_doc_url=None)
    mock_key.assert_called_once_with(doc)
    mock_get_str.assert_called_once_with("last_python_script_name_writer")
    assert msg["selected_script_name"] == "Prime"


def test_build_scripts_list_message_includes_selected_script_name_when_empty():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={"Alpha": "a = 1"}), patch(
        "plugin.framework.config.get_config_str", return_value=""
    ), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        msg = build_scripts_list_message(ctx, session_doc=doc, session_doc_url=None)
    assert msg["selected_script_name"] == "Alpha"


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


def test_build_scripts_list_untitled_save_is_not_stale():
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/saved.odt")
    attach_document_script(doc, "A", "x")
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.framework.config.get_config_str", return_value=""
    ), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        msg = build_scripts_list_message(ctx, session_doc=doc, session_doc_url="")
    assert msg["document_stale"] is False
    sections = {s["id"]: s["scripts"] for s in msg["sections"]}
    assert sections["document"]  # non-empty; name may be display-prefixed


def test_build_scripts_list_includes_analysis_section_for_calc():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.scripting.domain_registry.is_calc", return_value=True
    ), patch(
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
    assert parse_picker_display_name(ANALYSIS_SCRIPT_DISPLAY_PREFIX, display) == "describe_data"


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
    doc = MagicMock()
    with patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        items, merged, origin_map = build_xdl_script_picker_state(doc, {})
    for helper in ("extract_text", "extract_structure"):
        display = f"{VISION_SCRIPT_DISPLAY_PREFIX}{helper}"
        assert display in items
        assert display in merged
        assert origin_map[display] == SCRIPT_ORIGIN_VISION


def test_resolve_vision_script_picker_entry():
    display = f"{VISION_SCRIPT_DISPLAY_PREFIX}extract_text"
    origin_map = {display: SCRIPT_ORIGIN_VISION}
    assert resolve_script_picker_entry(display, origin_map) == ("extract_text", SCRIPT_ORIGIN_VISION)
    assert parse_picker_display_name(VISION_SCRIPT_DISPLAY_PREFIX, display) == "extract_text"


def test_build_scripts_list_excludes_text_analytics_section_for_writer():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.scripting.text_analytics.supports_text_analytics_manual", return_value=True
    ):
        msg = build_scripts_list_message(ctx, session_doc=doc, session_doc_url=None)
    section_ids = [s["id"] for s in msg["sections"]]
    assert "text" not in section_ids


def test_build_xdl_script_picker_excludes_text_analytics_for_writer():
    doc = MagicMock()
    with patch("plugin.scripting.text_analytics.supports_text_analytics_manual", return_value=True):
        items, merged, origin_map = build_xdl_script_picker_state(doc, {})
    text_items = [name for name in items if name.startswith("[Text] ")]
    assert text_items == []
    assert not any(origin == "text" for origin in origin_map.values())


def test_save_and_delete_user_script():
    store = {"Mine": "a = 1"}
    with patch("plugin.framework.config.get_config", side_effect=lambda key: store if key == "saved_python_scripts" else None), patch(
        "plugin.framework.config.set_config"
    ) as mock_set:
        save_user_script("New", "b = 2")
        mock_set.assert_called_with("saved_python_scripts", {"Mine": "a = 1", "New": "b = 2"})
        store["New"] = "b = 2"
        delete_user_script("Mine")
        mock_set.assert_called_with("saved_python_scripts", {"New": "b = 2"})


def test_handle_editor_script_message_unknown_kind():
    sent: list = []
    assert handle_editor_script_message("save", {}, ctx=MagicMock(), session_doc=None, session_doc_url=None, send=sent.append) is False
    assert sent == []


def test_handle_editor_script_message_save_user_and_empty_name():
    ctx = MagicMock()
    sent: list = []
    store = {"Mine": "a = 1"}
    with patch("plugin.framework.config.get_config", side_effect=lambda key: store if key == "saved_python_scripts" else {}), patch(
        "plugin.framework.config.set_config"
    ) as mock_set, patch("plugin.framework.config.get_config_str", return_value="Mine"), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        assert handle_editor_script_message(
            "save_script",
            {"name": "New", "code": "x = 1", "origin": "user"},
            ctx=ctx,
            session_doc=None,
            session_doc_url=None,
            send=sent.append,
        )
        mock_set.assert_any_call("saved_python_scripts", {"Mine": "a = 1", "New": "x = 1"})
        mock_set.assert_any_call("last_python_script_name_writer", "New")
        assert sent[-1]["type"] == "scripts_list"
        assert "Saved script" in sent[-1]["status_ok_text"]

        sent.clear()
        assert handle_editor_script_message(
            "save_script",
            {"name": "  ", "code": "x = 1"},
            ctx=ctx,
            session_doc=None,
            session_doc_url=None,
            send=sent.append,
        )
        assert sent[-1]["status_error_text"]


def test_handle_editor_script_message_save_document_script_updates_config():
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    sent: list = []
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.framework.config.set_config"
    ) as mock_set, patch("plugin.framework.config.get_config_str", return_value=""), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        assert handle_editor_script_message(
            "save_script",
            {"name": "DocReport", "code": "y = 2", "origin": "document"},
            ctx=ctx,
            session_doc=doc,
            session_doc_url=None,
            send=sent.append,
        )
        mock_set.assert_called_with("last_python_script_name_writer", "[Doc] DocReport")
        assert sent[-1]["type"] == "scripts_list"
        assert "Saved script 'DocReport' to this document" in sent[-1]["status_ok_text"]


def test_doc_save_fallback_sends_success_with_migration_note_only():
    """A document save error that lands in My Scripts is one success string."""
    ctx = MagicMock()
    doc = _DocWithUserDefinedProperties(_UserDefinedProperties())
    sent: list = []
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.framework.config.set_config"
    ) as mock_set, patch("plugin.framework.config.get_config_str", return_value=""), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ), patch(
        "plugin.scripting.document_scripts.save_document_script",
        return_value=DocumentScriptError("Document is read-only.", DocumentScriptErrorCode.READONLY),
    ):
        assert handle_editor_script_message(
            "save_script",
            {"name": "DocReport", "code": "y = 2", "origin": "document"},
            ctx=ctx,
            session_doc=doc,
            session_doc_url=None,
            send=sent.append,
        )
    mock_set.assert_any_call("saved_python_scripts", {"DocReport": "y = 2"})
    msg = sent[-1]
    assert msg["type"] == "scripts_list"
    assert "status_error_text" not in msg
    assert "Saved script 'DocReport' to My Scripts." in msg["status_ok_text"]
    assert "Document is read-only." in msg["status_ok_text"]


def test_doc_save_fallback_does_not_overwrite_my_scripts():
    """A document save error must not replace an existing My Scripts entry."""
    ctx = MagicMock()
    doc = _DocWithUserDefinedProperties(_UserDefinedProperties())
    sent: list = []
    store = {"DocReport": "keep-me"}

    def _get_config(key: str):
        if key == "saved_python_scripts":
            return dict(store)
        return {}

    with patch("plugin.framework.config.get_config", side_effect=_get_config), patch(
        "plugin.framework.config.set_config"
    ) as mock_set, patch("plugin.framework.config.get_config_str", return_value=""), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ), patch(
        "plugin.scripting.document_scripts.save_document_script",
        return_value=DocumentScriptError("Document is read-only.", DocumentScriptErrorCode.READONLY),
    ):
        assert handle_editor_script_message(
            "save_script",
            {"name": "DocReport", "code": "y = 2", "origin": "document"},
            ctx=ctx,
            session_doc=doc,
            session_doc_url=None,
            send=sent.append,
        )
    written = [call.args for call in mock_set.call_args_list if call.args and call.args[0] == "saved_python_scripts"]
    assert written == []
    msg = sent[-1]
    assert "status_ok_text" not in msg
    assert "already exists" in msg["status_error_text"]
    assert "Document is read-only." in msg["status_error_text"]
    assert store["DocReport"] == "keep-me"


def test_handle_editor_script_message_copy_updates_config_when_allowed():
    ctx = MagicMock()
    sent: list = []
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.framework.config.get_config_str", return_value=""
    ), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ), patch("plugin.framework.config.set_config") as mock_set:
        assert handle_editor_script_message(
            "copy_script_to_user",
            {"name": "CopiedScript", "code": "print(1)", "overwrite": False},
            ctx=ctx,
            session_doc=None,
            session_doc_url=None,
            send=sent.append,
        )
        mock_set.assert_any_call("saved_python_scripts", {"CopiedScript": "print(1)"})
        mock_set.assert_any_call("last_python_script_name_writer", "CopiedScript")


def test_handle_editor_script_message_copy_refuses_overwrite():
    ctx = MagicMock()
    sent: list = []
    with patch("plugin.framework.config.get_config", return_value={"Mine": "a = 1"}), patch(
        "plugin.framework.config.get_config_str", return_value="Mine"
    ), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ), patch("plugin.framework.config.set_config") as mock_set:
        handle_editor_script_message(
            "copy_script_to_user",
            {"name": "Mine", "code": "new", "overwrite": False},
            ctx=ctx,
            session_doc=None,
            session_doc_url=None,
            send=sent.append,
        )
        mock_set.assert_not_called()
        assert "already exists" in sent[-1]["status_error_text"]


def test_handle_editor_script_message_attach_requires_doc():
    sent: list = []
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.framework.config.get_config_str", return_value=""
    ), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        handle_editor_script_message(
            "attach_script",
            {"name": "A", "code": "x"},
            ctx=MagicMock(),
            session_doc=None,
            session_doc_url=None,
            send=sent.append,
        )
    assert sent[-1]["status_error_text"]


def test_attach_script_stores_display_name_as_property_key():
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    sent: list = []
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.framework.config.get_config_str", return_value=""
    ), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        assert handle_editor_script_message(
            "attach_script",
            {"name": "[Doc] Regional", "code": "doc-code", "overwrite": False},
            ctx=ctx,
            session_doc=doc,
            session_doc_url=None,
            send=sent.append,
        )
    stored = get_document_scripts(doc)
    assert stored["Regional"] == "doc-code"
    assert "[Doc] Regional" not in stored
    assert "Attached script 'Regional'" in sent[-1]["status_ok_text"]


def test_handle_editor_script_message_disposed_document_returns_list_error():
    """Closing the document mid-save must not raise out of the picker handler."""
    from plugin.framework.errors import DocumentDisposedError

    ctx = MagicMock()
    sent: list = []
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.framework.config.get_config_str", return_value=""
    ), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ), patch(
        "plugin.scripting.document_scripts.save_document_script",
        side_effect=DocumentDisposedError("gone"),
    ):
        assert (
            handle_editor_script_message(
                "save_script",
                {"name": "A", "code": "x = 1", "origin": "document"},
                ctx=ctx,
                session_doc=MagicMock(),
                session_doc_url="",
                send=sent.append,
            )
            is True
        )
    assert sent[-1]["type"] == "scripts_list"
    assert "closed" in sent[-1]["status_error_text"].lower()


def test_request_scripts_disposed_active_document_still_answers():
    from plugin.framework.errors import DocumentDisposedError

    sent: list = []
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.framework.config.get_config_str", return_value=""
    ), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ), patch(
        "plugin.scripting.document_scripts.get_active_document_for_scripts",
        side_effect=DocumentDisposedError("gone"),
    ):
        assert (
            handle_editor_script_message(
                "request_scripts",
                {},
                ctx=MagicMock(),
                session_doc=None,
                session_doc_url=None,
                send=sent.append,
            )
            is True
        )
    assert sent[-1]["type"] == "scripts_list"
    assert sent[-1]["document_available"] is False
    assert "closed" in sent[-1]["status_error_text"].lower()


def test_builtin_origin_delete_does_not_touch_user_scripts():
    sent: list[dict] = []
    with patch("plugin.scripting.document_scripts.delete_user_script") as mock_del:
        handle_editor_script_message(
            "delete_script",
            {"name": "Plot", "origin": "viz"},
            ctx=MagicMock(),
            session_doc=MagicMock(),
            session_doc_url="file:///a",
            send=sent.append,
        )
    mock_del.assert_not_called()
    assert "Copy to My Scripts" in sent[-1]["status_error_text"]


def test_stale_document_save_does_not_write():
    sent: list[dict] = []
    with (
        patch("plugin.scripting.document_scripts.document_scripts_identity", return_value="file:///other"),
        patch("plugin.scripting.document_scripts.save_document_script") as mock_save,
    ):
        handle_editor_script_message(
            "save_script",
            {"name": "A", "code": "x = 1", "origin": "document"},
            ctx=MagicMock(),
            session_doc=MagicMock(),
            session_doc_url="file:///original",
            send=sent.append,
        )
    mock_save.assert_not_called()
    assert "Document changed" in sent[-1]["status_error_text"]


def test_init_script_omitted_from_picker():
    with patch(
        "plugin.scripting.document_scripts.get_document_scripts",
        return_value={"INIT": "x = 1", "Hello": "y = 1"},
    ), patch(
        "plugin.scripting.domain_registry.get_picker_domains",
        return_value=(),
    ):
        items, merged, _origins = build_xdl_script_picker_state(MagicMock(), {})
    assert document_script_display_name("Hello") in items
    assert document_script_display_name("INIT") not in items
    assert "x = 1" not in merged.values()


def test_script_picker_message_types():
    assert "request_scripts" in SCRIPT_PICKER_MESSAGE_TYPES
    assert "save" not in SCRIPT_PICKER_MESSAGE_TYPES


def test_document_script_reopen_after_select_and_save_uses_display_key():
    """List, selection, and save share the [Doc] key so a same-named user script is not overwritten."""
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/test.odt")
    attach_document_script(doc, "Regional", "doc-code")
    config: dict[str, object] = {
        "saved_python_scripts": {"Regional": "user-code"},
        "last_python_script_name_writer": "",
    }

    def _get_config(key: str):
        return config.get(key)

    def _get_config_str(key: str) -> str:
        value = config.get(key)
        return value if isinstance(value, str) else ""

    def _set_config(key: str, value: object) -> None:
        config[key] = value

    with patch("plugin.framework.config.get_config", side_effect=_get_config), patch(
        "plugin.framework.config.get_config_str", side_effect=_get_config_str
    ), patch("plugin.framework.config.set_config", side_effect=_set_config), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        listed = build_scripts_list_message(ctx, session_doc=doc, session_doc_url="file:///tmp/test.odt")
        sections = {section["id"]: section["scripts"] for section in listed["sections"]}
        assert sections["user"]["Regional"] == "user-code"
        assert sections["document"]["[Doc] Regional"] == "doc-code"
        assert "Regional" not in sections["document"]

        sent: list[dict] = []
        assert handle_editor_script_message(
            "select_script",
            {"name": "[Doc] Regional"},
            ctx=ctx,
            session_doc=doc,
            session_doc_url="file:///tmp/test.odt",
            send=sent.append,
        )
        reopened = build_scripts_list_message(ctx, session_doc=doc, session_doc_url="file:///tmp/test.odt")
        assert reopened["selected_script_name"] == "[Doc] Regional"
        re_sections = {section["id"]: section["scripts"] for section in reopened["sections"]}
        assert re_sections["document"][reopened["selected_script_name"]] == "doc-code"

        assert handle_editor_script_message(
            "save_script",
            {"name": "[Doc] Regional", "code": "doc-new", "origin": "document"},
            ctx=ctx,
            session_doc=doc,
            session_doc_url="file:///tmp/test.odt",
            send=sent.append,
        )
    stored = get_document_scripts(doc)
    assert stored["Regional"] == "doc-new"
    assert "[Doc] Regional" not in stored
    assert config["saved_python_scripts"] == {"Regional": "user-code"}
    assert config["last_python_script_name_writer"] == "[Doc] Regional"


def test_document_scripts_uno_skips_windows_leftover_hidden_reopen() -> None:
    """GHA 34679494812: leftover_open=3 create_native_doc uid=41 then 30s hang."""
    src = Path(__file__).with_name("test_document_scripts_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "document scripts Hidden _blank reopen" in src
    assert "34679494812" in src


def test_monaco_overwrite_check_uses_document_display_key():
    """New/Save As types a storage name; This Document list keys are [Doc] labels.

    Probing the raw name missed the row, so the overwrite confirm never ran
    and save_document_script replaced the existing script.
    """
    js_path = (
        Path(__file__).resolve().parents[2]
        / "plugin/contrib/scripting/assets/editor/scripts_manager.js"
    )
    js = js_path.read_text(encoding="utf-8")
    key_fn = re.search(
        r"function documentScriptListKey\(name\) \{\n"
        r"    var prefix = \"([^\"]*)\";\n"
        r"    return name\.indexOf\(prefix\) === 0 \? name : prefix \+ name;\n"
        r"  \}",
        js,
    )
    exists_fn = re.search(
        r"function scriptExistsInSection\(sectionId, name\) \{\n"
        r"    var lookup = sectionId === \"document\" \? documentScriptListKey\(name\) : name;\n"
        r"    for \(var s = 0; s < scriptSections\.length; s\+\+\) \{\n"
        r"      if \(scriptSections\[s\]\.id === sectionId\) \{\n"
        r"        var scripts = scriptSections\[s\]\.scripts \|\| \{\};\n"
        r"        return scripts\[lookup\] !== undefined;\n"
        r"      \}\n"
        r"    \}\n"
        r"    return false;\n"
        r"  \}",
        js,
    )
    assert key_fn is not None
    assert exists_fn is not None
    prefix = key_fn.group(1)
    assert prefix == DOC_SCRIPT_DISPLAY_PREFIX

    def list_key(name: str) -> str:
        return name if name.startswith(prefix) else prefix + name

    def exists_in_section(sections: list[dict], section_id: str, name: str) -> bool:
        lookup = list_key(name) if section_id == "document" else name
        for section in sections:
            if section["id"] == section_id:
                return lookup in (section.get("scripts") or {})
        return False

    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/test.odt")
    attach_document_script(doc, "Regional", "result = 3")
    with patch("plugin.framework.config.get_config", return_value={"Regional": "user-code"}), patch(
        "plugin.framework.config.get_config_str", return_value=""
    ), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        msg = build_scripts_list_message(ctx, session_doc=doc, session_doc_url="file:///tmp/test.odt")
    sections = msg["sections"]
    by_id = {section["id"]: section["scripts"] for section in sections}
    # The raw storage name is not a list key. The picker must still find it.
    assert "Regional" not in by_id["document"]
    assert by_id["document"][document_script_display_name("Regional")] == "result = 3"
    assert exists_in_section(sections, "document", "Regional")
    assert exists_in_section(sections, "document", document_script_display_name("Regional"))
    assert not exists_in_section(sections, "document", "Other")
    # My Scripts keeps the raw name, including when it matches a document script.
    assert exists_in_section(sections, "user", "Regional")
    assert not exists_in_section(sections, "user", document_script_display_name("Regional"))


def test_set_calc_init_script_returns_the_property_write_result() -> None:
    """No host-side init cache to refresh: the result is the document write's."""
    from plugin.scripting import document_scripts as ds

    with (
        patch.object(ds, "get_document_scripts", return_value={}),
        patch.object(ds, "set_document_scripts", return_value="read-only") as write,
    ):
        assert ds.set_calc_init_script(MagicMock(), "x = 1") == "read-only"
    assert write.call_args[0][1] == {"INIT": "x = 1"}


def test_calc_init_script_both_keys_present_roundtrip():
    """Bug 1: when both INIT and Init exist, set_calc_init_script normalizes to a single INIT."""
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    set_document_scripts(doc, {"INIT": "init_upper", "Init": "stale_init", "Other": "result = 1"})
    assert get_calc_init_script(doc) == "init_upper"
    assert set_calc_init_script(doc, "new_init_code") is None
    scripts = get_document_scripts(doc)
    assert scripts["INIT"] == "new_init_code"
    assert "Init" not in scripts
    assert scripts["Other"] == "result = 1"
    assert get_calc_init_script(doc) == "new_init_code"


def test_calc_init_script_empty_init_does_not_fall_through_to_stale_init():
    """Bug 1: an empty INIT key takes precedence over a stale Init key."""
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    set_document_scripts(doc, {"INIT": "", "Init": "stale_init"})
    assert get_calc_init_script(doc) == ""


def test_build_scripts_list_message_stale_doc_does_not_select_doc_script():
    """Bug 2: stale doc passes None to selection resolution so no [Doc] script is selected."""
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/opened.ods")
    attach_document_script(doc, "SecretDocScript", "secret = 1")
    with patch("plugin.framework.config.get_config", return_value={"UserScript": "x = 1"}), patch(
        "plugin.framework.config.get_config_str", return_value="[Doc] SecretDocScript"
    ), patch(
        "plugin.framework.config.set_config"
    ) as mock_set, patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_calc",
    ):
        # session_doc_url is different from doc.getURL(), marking document_stale = True
        msg = build_scripts_list_message(
            ctx,
            session_doc=doc,
            session_doc_url="file:///tmp/other.ods",
        )
    assert msg["document_stale"] is True
    assert msg["selected_script_name"] != "[Doc] SecretDocScript"
    assert not msg["selected_script_name"].startswith("[Doc] ")
    # Ensure set_config did not store "[Doc] SecretDocScript" against the stale doc
    for call in mock_set.call_args_list:
        assert call.args[1] != "[Doc] SecretDocScript"


def test_save_script_reserved_name_init_does_not_fallback_to_my_scripts():
    """Bug 3: reserving name INIT returns an error to user and does not write to My Scripts."""
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/doc.ods")
    sent: list = []
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.framework.config.set_config"
    ) as mock_set, patch("plugin.framework.config.get_config_str", return_value=""), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_calc",
    ):
        handled = handle_editor_script_message(
            "save_script",
            {"name": "INIT", "code": "malicious = 1", "origin": "document"},
            ctx=ctx,
            session_doc=doc,
            session_doc_url="file:///tmp/doc.ods",
            send=sent.append,
        )
    assert handled is True
    assert len(sent) == 1
    assert "status_ok_text" not in sent[0]
    assert "reserved" in sent[0].get("status_error_text", "").lower()
    # Must NOT have written to My Scripts (saved_python_scripts)
    user_writes = [c for c in mock_set.call_args_list if c.args and c.args[0] == "saved_python_scripts"]
    assert user_writes == []


def test_save_script_too_large_does_not_fallback_to_my_scripts():
    """Bug 3: a script exceeding document size limits does not fall back to My Scripts."""
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/doc.ods")
    sent: list = []
    huge_code = "x = 1\n" * 200_000
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.framework.config.set_config"
    ) as mock_set, patch("plugin.framework.config.get_config_str", return_value=""), patch(
        "plugin.scripting.python_runner.resolve_run_script_name_config_key",
        return_value="last_python_script_name_calc",
    ):
        handled = handle_editor_script_message(
            "save_script",
            {"name": "TooLarge", "code": huge_code, "origin": "document"},
            ctx=ctx,
            session_doc=doc,
            session_doc_url="file:///tmp/doc.ods",
            send=sent.append,
        )
    assert handled is True
    assert len(sent) == 1
    assert "status_ok_text" not in sent[0]
    assert "too large" in sent[0].get("status_error_text", "").lower()
    user_writes = [c for c in mock_set.call_args_list if c.args and c.args[0] == "saved_python_scripts"]
    assert user_writes == []


def test_delete_document_script_missing_returns_error_and_does_not_write():
    """Bug 4: deleting a missing document script returns an error and does not touch properties."""
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    err = delete_document_script(doc, "NonExistent")
    assert err is not None
    assert err.code == DocumentScriptErrorCode.NOT_FOUND
    assert DOCUMENT_SCRIPTS_UDPROP not in props.values


def test_delete_user_script_missing_returns_error_and_does_not_write():
    """Bug 4: deleting a missing user script returns an error and does not rewrite config."""
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.framework.config.set_config"
    ) as mock_set:
        err = delete_user_script("NonExistent")
    assert err is not None
    assert err.code == DocumentScriptErrorCode.NOT_FOUND
    mock_set.assert_not_called()


def test_picker_delete_missing_script_returns_error():
    """Bug 4: IPC message delete_script for non-existent script returns status_error_text."""
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/doc.ods")

    # Document delete missing
    sent_doc: list = []
    handle_editor_script_message(
        "delete_script",
        {"name": "NonExistentDoc", "origin": "document"},
        ctx=ctx,
        session_doc=doc,
        session_doc_url="file:///tmp/doc.ods",
        send=sent_doc.append,
    )
    assert "status_ok_text" not in sent_doc[-1]
    assert "does not exist" in sent_doc[-1].get("status_error_text", "")

    # User delete missing
    sent_user: list = []
    with patch("plugin.framework.config.get_config", return_value={}):
        handle_editor_script_message(
            "delete_script",
            {"name": "NonExistentUser", "origin": "user"},
            ctx=ctx,
            session_doc=doc,
            session_doc_url="file:///tmp/doc.ods",
            send=sent_user.append,
        )
    assert "status_ok_text" not in sent_user[-1]
    assert "does not exist" in sent_user[-1].get("status_error_text", "")


def test_enumerate_calc_documents_skips_disposed_element_and_continues():
    """Cleanup: per-element try/except in _enumerate_calc_documents continues on broken component."""
    broken_elem = MagicMock()
    broken_elem.getURL.side_effect = RuntimeError("disposed")
    broken_elem.getController.side_effect = RuntimeError("disposed")

    valid_calc = MagicMock()
    valid_calc.getURL.return_value = "file:///valid.ods"

    comps = _calc_component_enum(broken_elem, valid_calc)
    desktop = MagicMock()
    desktop.getComponents.return_value = comps

    with patch("plugin.scripting.document_scripts.is_calc", side_effect=lambda m: m is valid_calc):
        from plugin.scripting.document_scripts import _enumerate_calc_documents

        results = _enumerate_calc_documents(desktop)
    assert results == [valid_calc]


def test_shared_save_selected_script_user_and_doc_and_builtin():
    """Cleanup: shared save_selected_script helper handles user, doc, and template entries."""
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    attach_document_script(doc, "DocScript", "x = 1")

    with patch("plugin.scripting.document_scripts.get_user_scripts", return_value={"UserScript": "u = 1"}), patch(
        "plugin.scripting.document_scripts.save_user_script"
    ) as mock_user_save, patch(
        "plugin.scripting.document_scripts.save_document_script",
        return_value=None,
    ) as mock_doc_save:
        # Save to user script
        err = save_selected_script(doc, "UserScript", "u = 2")
        assert err is None
        mock_user_save.assert_called_once_with("UserScript", "u = 2")

        # Save to document script
        err = save_selected_script(doc, "[Doc] DocScript", "x = 2")
        assert err is None
        mock_doc_save.assert_called_once_with(doc, "DocScript", "x = 2")

        # Builtin template refusal when allow_builtin_skip=False
        with patch("plugin.scripting.document_scripts.is_picker_template_name", return_value=True):
            err = save_selected_script(doc, "[Vision] extract_text", "code", allow_builtin_skip=False)
            assert err is not None
            assert "read-only" in err.lower()

            # Builtin template skip when allow_builtin_skip=True
            err = save_selected_script(doc, "[Vision] extract_text", "code", allow_builtin_skip=True)
            assert err is None

        # Missing script error
        with patch("plugin.scripting.document_scripts.is_picker_template_name", return_value=False):
            err = save_selected_script(doc, "NonExistent", "code")
            assert err is not None
            assert "not in My Scripts or this document" in err

