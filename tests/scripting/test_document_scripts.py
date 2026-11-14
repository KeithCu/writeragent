# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for document-attached Run Python Script storage."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


from plugin.scripting.document_scripts import (
    DOCUMENT_SCRIPTS_UDPROP,
    _MAX_DOCUMENT_SCRIPTS_BYTES,
    _closed_document_scripts_list,
    DocumentScriptError,
    DocumentScriptErrorCode,
    attach_document_script,
    build_scripts_list_message,
    build_xdl_script_picker_state,
    delete_document_script,
    delete_script_from_origin,
    delete_user_script,
    document_script_display_name,
    document_scripts_identity,
    document_scripts_write_is_stale,
    get_calc_document_from_ctx,
    get_calc_init_script,
    get_document_scripts,
    get_user_scripts,
    handle_editor_script_message,
    new_script_name_error,
    resolve_run_script_selection,
    resolve_script_picker_entry,
    save_script_to_origin,
    save_selected_script,
    save_user_script,
    set_calc_init_script,
    set_document_scripts,
)
from plugin.framework.i18n import _
from plugin.scripting.domain_registry import (
    ANALYSIS_SCRIPT_DISPLAY_PREFIX,
    SCRIPT_ORIGIN_ANALYSIS,
    SCRIPT_ORIGIN_DOCUMENT,
    SCRIPT_ORIGIN_USER,
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


@pytest.mark.parametrize(("writer_flag", "draw_flag"), [(True, False), (False, True)])
def test_get_calc_document_from_ctx_does_not_fall_back_from_other_app(writer_flag: bool, draw_flag: bool):
    doc = MagicMock()
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = doc
    with (
        patch("plugin.scripting.document_scripts.get_desktop", return_value=desktop),
        patch("plugin.scripting.document_scripts.is_writer", return_value=writer_flag),
        patch("plugin.scripting.document_scripts.is_draw", return_value=draw_flag),
        patch("plugin.scripting.document_scripts.is_calc", return_value=False),
    ):
        assert get_calc_document_from_ctx(MagicMock()) is None
    desktop.getComponents.assert_not_called()


def test_get_calc_document_from_ctx_enumerates_from_base():
    base_doc = MagicMock()
    calc = MagicMock()
    calc.getURL.return_value = "file:///only.ods"
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = base_doc
    desktop.getComponents.return_value = _calc_component_enum(calc)
    with (
        patch("plugin.scripting.document_scripts.get_desktop", return_value=desktop),
        patch("plugin.scripting.document_scripts.is_writer", return_value=False),
        patch("plugin.scripting.document_scripts.is_draw", return_value=False),
        patch("plugin.scripting.document_scripts.is_calc", side_effect=lambda model: model is calc),
    ):
        assert get_calc_document_from_ctx(MagicMock()) is calc
    desktop.getComponents.assert_called_once()


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


def test_unreadable_envelope_is_not_replaced_by_attach_or_init():
    props = _UserDefinedProperties()
    props.values[DOCUMENT_SCRIPTS_UDPROP] = "not-json"
    doc = _DocWithUserDefinedProperties(props)

    err = attach_document_script(doc, "A", "code")
    assert err is not None
    assert err.code == DocumentScriptErrorCode.UNREADABLE
    assert props.values[DOCUMENT_SCRIPTS_UDPROP] == "not-json"

    err = set_calc_init_script(doc, "init")
    assert err is not None
    assert err.code == DocumentScriptErrorCode.UNREADABLE
    assert props.values[DOCUMENT_SCRIPTS_UDPROP] == "not-json"

    err = delete_document_script(doc, "A")
    assert err is not None
    assert err.code == DocumentScriptErrorCode.UNREADABLE
    assert props.values[DOCUMENT_SCRIPTS_UDPROP] == "not-json"


def test_future_version_envelope_is_not_replaced():
    raw = json.dumps({"version": 99, "scripts": {"Keep": "x = 1"}})
    props = _UserDefinedProperties()
    props.values[DOCUMENT_SCRIPTS_UDPROP] = raw
    doc = _DocWithUserDefinedProperties(props)

    err = attach_document_script(doc, "New", "x = 2")
    assert err is not None
    assert err.code == DocumentScriptErrorCode.UNREADABLE
    assert props.values[DOCUMENT_SCRIPTS_UDPROP] == raw
    assert get_document_scripts(doc) == {}


def test_non_string_script_entry_is_unreadable():
    raw = json.dumps({"version": 1, "scripts": {"A": "ok", "B": 1}})
    props = _UserDefinedProperties()
    props.values[DOCUMENT_SCRIPTS_UDPROP] = raw
    doc = _DocWithUserDefinedProperties(props)

    assert get_document_scripts(doc) == {}
    err = attach_document_script(doc, "C", "y")
    assert err is not None
    assert err.code == DocumentScriptErrorCode.UNREADABLE
    assert props.values[DOCUMENT_SCRIPTS_UDPROP] == raw


def test_python_literal_envelope_is_unreadable():
    raw = "{'version': 1, 'scripts': {'A': 'x'}}"
    props = _UserDefinedProperties()
    props.values[DOCUMENT_SCRIPTS_UDPROP] = raw
    doc = _DocWithUserDefinedProperties(props)

    err = attach_document_script(doc, "A", "y")
    assert err is not None
    assert err.code == DocumentScriptErrorCode.UNREADABLE
    assert props.values[DOCUMENT_SCRIPTS_UDPROP] == raw


def test_set_document_scripts_still_replaces_unreadable_property():
    props = _UserDefinedProperties()
    props.values[DOCUMENT_SCRIPTS_UDPROP] = "not-json"
    doc = _DocWithUserDefinedProperties(props)

    err = set_document_scripts(doc, {"A": "x"})
    assert err is None
    assert get_document_scripts(doc) == {"A": "x"}


def test_blank_property_is_not_unreadable():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    err = attach_document_script(doc, "A", "x")
    assert err is None
    assert get_document_scripts(doc) == {"A": "x"}

    props2 = _UserDefinedProperties()
    props2.values[DOCUMENT_SCRIPTS_UDPROP] = ""
    doc2 = _DocWithUserDefinedProperties(props2)
    err2 = attach_document_script(doc2, "B", "y")
    assert err2 is None
    assert get_document_scripts(doc2) == {"B": "y"}


def test_missing_version_stays_unreadable_and_unsupported_version_names_the_version():
    raw_missing = json.dumps({"scripts": {"A": "x"}})
    props1 = _UserDefinedProperties()
    props1.values[DOCUMENT_SCRIPTS_UDPROP] = raw_missing
    doc1 = _DocWithUserDefinedProperties(props1)
    err1 = attach_document_script(doc1, "B", "y")
    assert getattr(err1, "code", None) == DocumentScriptErrorCode.UNREADABLE
    assert props1.values[DOCUMENT_SCRIPTS_UDPROP] == raw_missing

    raw_v99 = json.dumps({"version": 99, "scripts": {"Keep": "x = 1"}})
    props2 = _UserDefinedProperties()
    props2.values[DOCUMENT_SCRIPTS_UDPROP] = raw_v99
    doc2 = _DocWithUserDefinedProperties(props2)
    err2 = attach_document_script(doc2, "B", "y")
    assert getattr(err2, "code", None) == DocumentScriptErrorCode.UNREADABLE
    assert "99" in str(err2)
    assert props2.values[DOCUMENT_SCRIPTS_UDPROP] == raw_v99


def test_scripts_list_reports_unreadable_envelope():
    raw = json.dumps({"version": 99, "scripts": {"Keep": "x = 1"}})
    props = _UserDefinedProperties()
    props.values[DOCUMENT_SCRIPTS_UDPROP] = raw
    doc = _DocWithUserDefinedProperties(props)

    with (
        patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""),
        patch("plugin.scripting.domain_registry.PICKER_WIRING", ()),
    ):
        msg = build_scripts_list_message(
            session_doc=doc,
            session_doc_url=None,
        )
    assert msg["document_available"] is True
    doc_section = [s for s in msg["sections"] if s["id"] == "document"][0]
    assert doc_section["scripts"] == {}
    assert "99" in (msg.get("status_error_text") or "")

    with (
        patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""),
        patch("plugin.scripting.domain_registry.PICKER_WIRING", ()),
    ):
        msg_ok = build_scripts_list_message(
            session_doc=doc,
            session_doc_url=None,
            status_ok_text="All good",
        )
    assert msg_ok.get("status_ok_text") == "All good"
    assert "status_error_text" not in msg_ok

    props_corrupt = _UserDefinedProperties()
    props_corrupt.values[DOCUMENT_SCRIPTS_UDPROP] = "not-json"
    doc_corrupt = _DocWithUserDefinedProperties(props_corrupt)
    with (
        patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""),
        patch("plugin.scripting.domain_registry.PICKER_WIRING", ()),
    ):
        msg_corrupt = build_scripts_list_message(
            session_doc=doc_corrupt,
            session_doc_url=None,
        )
    corrupt_section = [s for s in msg_corrupt["sections"] if s["id"] == "document"][0]
    assert corrupt_section["scripts"] == {}
    assert "unreadable" in msg_corrupt.get("status_error_text", "").lower()


def test_attach_empty_and_reserved_name_do_not_store_the_error_code():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    err_empty = attach_document_script(doc, "", "body")
    assert getattr(err_empty, "code", None) == DocumentScriptErrorCode.EMPTY_NAME
    assert DOCUMENT_SCRIPTS_UDPROP not in props.values
    assert get_document_scripts(doc) == {}

    err_reserved = attach_document_script(doc, "[Doc] x", "body")
    assert getattr(err_reserved, "code", None) == DocumentScriptErrorCode.RESERVED_NAME
    assert DOCUMENT_SCRIPTS_UDPROP not in props.values
    scripts = get_document_scripts(doc)
    assert "empty_name" not in scripts
    assert "reserved_name" not in scripts


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


def test_new_script_name_error_matches_attach_and_allows_user_init():
    assert new_script_name_error("", attach=True) == _("Script name cannot be empty.")
    assert new_script_name_error("", attach=False) == _("Script name cannot be empty.")
    assert new_script_name_error("  ", attach=True) is None

    err_prefix = new_script_name_error("[Doc] x", attach=False)
    assert err_prefix is not None
    assert "reserved prefix" in err_prefix and "[Doc] x" in err_prefix

    err_init_upper = new_script_name_error("INIT", attach=True)
    assert err_init_upper is not None and "workbook init" in err_init_upper
    err_init_title = new_script_name_error("Init", attach=True)
    assert err_init_title is not None and "workbook init" in err_init_title

    assert new_script_name_error("INIT", attach=False) is None
    assert new_script_name_error("Init", attach=False) is None
    assert new_script_name_error("init", attach=True) is None
    assert new_script_name_error("MyScript", attach=True) is None

    doc = MagicMock()
    err_attach = attach_document_script(doc, "INIT", "result = 1")
    assert err_attach is not None
    assert err_attach.code == DocumentScriptErrorCode.RESERVED_NAME
    assert "workbook init" in err_attach

    with (
        patch("plugin.framework.config.get_config", return_value={}),
        patch("plugin.framework.config.save") as mock_set,
    ):
        assert save_user_script("INIT", "x = 1") is None
        mock_set.assert_called_once()
        cfg_patch = mock_set.call_args[0][0]
        assert "saved_python_scripts" in cfg_patch
        assert "INIT" in cfg_patch["saved_python_scripts"]


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


def test_resolve_run_script_name_config_key():
    from plugin.scripting.document_scripts import resolve_run_script_name_config_key

    calc_doc = MagicMock()
    calc_doc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    assert resolve_run_script_name_config_key(calc_doc) == "last_python_script_name_calc"

    writer_doc = MagicMock()
    writer_doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    assert resolve_run_script_name_config_key(writer_doc) == "last_python_script_name_writer"

    draw_doc = MagicMock()
    draw_doc.supportsService.side_effect = lambda s: s == "com.sun.star.drawing.DrawingDocument"
    assert resolve_run_script_name_config_key(draw_doc) == "last_python_script_name_draw"

    assert resolve_run_script_name_config_key(None) == "last_python_script_name_writer"


def test_resolve_run_script_selection_uses_config_default():
    doc = MagicMock()
    saved = {
        "Hello WriterAgent": "result = 'hello'",
        "Prime Numbers": "result = 'gaps'",
    }
    with patch("plugin.framework.config.get_config", return_value="Prime Numbers"), patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        name, code, merged = resolve_run_script_selection(
            doc, build_xdl_script_picker_state(doc, saved)
        )
    assert name == "Prime Numbers"
    assert code == "result = 'gaps'"
    assert merged["Prime Numbers"] == "result = 'gaps'"


def test_resolve_run_script_selection_falls_back_to_first_name():
    doc = MagicMock()
    saved = {"Alpha": "a = 1", "Beta": "b = 2"}
    with patch("plugin.framework.config.get_config", return_value="Missing"), patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ), patch("plugin.framework.config.save") as mock_set:
        name, code, merged = resolve_run_script_selection(
            doc, build_xdl_script_picker_state(doc, saved)
        )
    assert name == "Alpha"
    assert code == "a = 1"
    mock_set.assert_called_once_with({"last_python_script_name_writer": "Alpha"})


def test_resolve_script_picker_entry():
    origin_map = {"Mine": "user", "[Doc] Shared": "document"}
    assert resolve_script_picker_entry("Mine", origin_map) == ("Mine", "user")
    assert resolve_script_picker_entry("[Doc] Shared", origin_map) == ("Shared", "document")


def test_build_scripts_list_message_sections():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/test.odt")
    attach_document_script(doc, "Regional", "result = 3")
    with patch("plugin.framework.config.get_config", side_effect=lambda k: {"Prime": "result = 2"} if k == "saved_python_scripts" else ""), patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key", return_value="last_python_script_name_writer"
    ):
        msg = build_scripts_list_message(session_doc=doc, session_doc_url="file:///tmp/test.odt")
    assert msg["document_available"] is True
    assert msg["document_stale"] is False
    sections = {s["id"]: s["scripts"] for s in msg["sections"]}
    assert sections["user"] == {"Prime": "result = 2"}
    assert sections["document"] == {"[Doc] Regional": "result = 3"}
    section_ids = [s["id"] for s in msg["sections"]]
    assert section_ids[0] == "user"
    assert section_ids[1] == "document"


def test_build_scripts_list_section_order_local_doc_vision_math_units_analysis():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/test.ods")
    doc.supportsService = MagicMock(side_effect=lambda s: s == "com.sun.star.sheet.SpreadsheetDocument")
    attach_document_script(doc, "SheetHelper", "result = 1")
    with patch("plugin.framework.config.get_config", side_effect=lambda k: {"MyLocal": "result = 0"} if k == "saved_python_scripts" else ""), patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key", return_value="last_python_script_name_calc"
    ), patch(
        "plugin.vision.vision_runner.supports_vision_manual", return_value=True
    ):
        msg = build_scripts_list_message(session_doc=doc, session_doc_url="file:///tmp/test.ods")
    section_ids = [s["id"] for s in msg["sections"]]
    # Must be local ("user"), then document ("document"), then vision, math, units, analysis, and the rest
    assert section_ids[:6] == ["user", "document", "vision", "math", "units", "analysis"]


def test_build_scripts_list_message_includes_selected_script():
    doc = MagicMock()
    def _cfg_selected(k):
        return {"Prime": "print('primes')"} if k == "saved_python_scripts" else "Prime"
    with patch("plugin.framework.config.get_config", side_effect=_cfg_selected) as mock_get, patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key", return_value="last_python_script_name_writer"
    ) as mock_key:
        msg = build_scripts_list_message(session_doc=doc, session_doc_url=None)
    mock_key.assert_called_once_with(doc)
    mock_get.assert_any_call("last_python_script_name_writer")
    assert msg["selected_script_name"] == "Prime"


def test_build_scripts_list_message_includes_selected_script_name_when_empty():
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", side_effect=lambda k: {"Alpha": "a = 1"} if k == "saved_python_scripts" else ""), patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        msg = build_scripts_list_message(session_doc=doc, session_doc_url=None)
    assert msg["selected_script_name"] == "Alpha"


def test_build_scripts_list_message_stale_when_url_changes():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/other.odt")
    attach_document_script(doc, "A", "x")
    with patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""), patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key", return_value="last_python_script_name_writer"
    ):
        msg = build_scripts_list_message(session_doc=doc, session_doc_url="file:///tmp/original.odt")
    assert msg["document_stale"] is True
    sections = {s["id"]: s["scripts"] for s in msg["sections"]}
    assert sections["document"] == {}


def test_build_scripts_list_untitled_save_is_not_stale():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/saved.odt")
    attach_document_script(doc, "A", "x")
    with patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""), patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        msg = build_scripts_list_message(session_doc=doc, session_doc_url="")
    assert msg["document_stale"] is False
    sections = {s["id"]: s["scripts"] for s in msg["sections"]}
    assert sections["document"]  # non-empty; name may be display-prefixed


def test_build_scripts_list_includes_analysis_section_for_calc():
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.framework.doc_type.is_calc", return_value=True
    ), patch(
        "plugin.scripting.document_scripts.is_calc", return_value=True
    ):
        msg = build_scripts_list_message(session_doc=doc, session_doc_url=None)
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
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.vision.vision_runner.supports_vision_manual", return_value=True
    ):
        msg = build_scripts_list_message(session_doc=doc, session_doc_url=None)
    section_ids = [s["id"] for s in msg["sections"]]
    assert SCRIPT_ORIGIN_VISION in section_ids
    vision = next(s for s in msg["sections"] if s["id"] == SCRIPT_ORIGIN_VISION)
    assert f"{VISION_SCRIPT_DISPLAY_PREFIX}extract_text" in vision["scripts"]
    assert f"{VISION_SCRIPT_DISPLAY_PREFIX}extract_structure" in vision["scripts"]


def test_build_scripts_list_includes_vision_section_for_calc():
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.scripting.document_scripts.is_calc", return_value=True
    ), patch("plugin.scripting.document_scripts.is_writer", return_value=False), patch(
        "plugin.vision.vision_runner.supports_vision_manual", return_value=True
    ):
        msg = build_scripts_list_message(session_doc=doc, session_doc_url=None)
    section_ids = [s["id"] for s in msg["sections"]]
    assert SCRIPT_ORIGIN_VISION in section_ids
    vision = next(s for s in msg["sections"] if s["id"] == SCRIPT_ORIGIN_VISION)
    assert f"{VISION_SCRIPT_DISPLAY_PREFIX}extract_text" in vision["scripts"]


def test_build_scripts_list_excludes_vision_section_for_draw():
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={}), patch(
        "plugin.scripting.document_scripts.is_draw", return_value=True
    ), patch("plugin.scripting.document_scripts.is_calc", return_value=False), patch(
        "plugin.scripting.document_scripts.is_writer", return_value=False
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=False):
        msg = build_scripts_list_message(session_doc=doc, session_doc_url=None)
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
    doc = MagicMock()
    with patch("plugin.framework.config.get_config", return_value={}):
        msg = build_scripts_list_message(session_doc=doc, session_doc_url=None)
    section_ids = [s["id"] for s in msg["sections"]]
    assert "text" not in section_ids


def test_build_xdl_script_picker_excludes_text_analytics_for_writer():
    doc = MagicMock()
    items, merged, origin_map = build_xdl_script_picker_state(doc, {})
    text_items = [name for name in items if name.startswith("[Text] ")]
    assert text_items == []
    assert not any(origin == "text" for origin in origin_map.values())


def test_save_and_delete_user_script():
    store = {"Mine": "a = 1"}
    with patch("plugin.framework.config.get_config", side_effect=lambda key: store if key == "saved_python_scripts" else None), patch(
        "plugin.framework.config.save"
    ) as mock_set:
        save_user_script("New", "b = 2")
        mock_set.assert_called_with({"saved_python_scripts": {"Mine": "a = 1", "New": "b = 2"}})
        store["New"] = "b = 2"
        delete_user_script("Mine")
        mock_set.assert_called_with({"saved_python_scripts": {"New": "b = 2"}})


def test_handle_editor_script_message_unknown_kind():
    sent: list = []
    assert handle_editor_script_message("save", {}, session_doc=None, session_doc_url=None, send=sent.append) is False
    assert sent == []


def test_handle_editor_script_message_save_user_and_empty_name():
    sent: list = []
    store = {"Mine": "a = 1"}
    with patch("plugin.framework.config.get_config", side_effect=lambda key: store if key == "saved_python_scripts" else "Mine"), patch(
        "plugin.framework.config.save"
    ) as mock_set, patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        assert handle_editor_script_message(
            "save_script",
            {"name": "New", "code": "x = 1", "origin": "user"},
            session_doc=None,
            session_doc_url=None,
            send=sent.append,
        )
        mock_set.assert_any_call({"saved_python_scripts": {"Mine": "a = 1", "New": "x = 1"}})
        mock_set.assert_any_call({"last_python_script_name_writer": "New"})
        assert sent[-1]["type"] == "scripts_list"
        assert sent[-1]["status_ok_text"]

        sent.clear()
        assert handle_editor_script_message(
            "save_script",
            {"name": "  ", "code": "x = 1"},
            session_doc=None,
            session_doc_url=None,
            send=sent.append,
        )
        assert sent[-1]["status_error_text"]


def test_handle_editor_script_message_save_document_script_updates_config():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    sent: list = []
    with patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""), patch(
        "plugin.framework.config.save"
    ) as mock_set, patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        assert handle_editor_script_message(
            "save_script",
            {"name": "DocReport", "code": "y = 2", "origin": "document"},
            session_doc=doc,
            session_doc_url=None,
            send=sent.append,
        )
        mock_set.assert_called_with({"last_python_script_name_writer": "[Doc] DocReport"})
        assert sent[-1]["type"] == "scripts_list"
        assert sent[-1]["status_ok_text"]


def test_readonly_document_every_saver_fails():
    """On a read-only document, every saver fails with the document read-only sentence and writes nothing."""
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.isReadonly = MagicMock(return_value=False)
    assert set_document_scripts(doc, {"Report": "old"}) is None
    doc.isReadonly.return_value = True

    config_store = {
        "saved_python_scripts": {"Report": "keep-me"},
        "last_python_script_name_writer": "[Doc] Report",
    }

    def _get_config(key: str, default=None):
        if key == "saved_python_scripts":
            return dict(config_store.get("saved_python_scripts", {}))
        return config_store.get(key, "" if default is None else default)

    def _save(patch_dict, *, event_key=None):
        config_store.update(patch_dict)

    sent: list = []
    with (
        patch("plugin.framework.config.get_config", side_effect=_get_config),
        patch("plugin.framework.config.save", side_effect=_save),
        patch(
            "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
            return_value="last_python_script_name_writer",
        ),
        patch("plugin.scripting.domain_registry.PICKER_WIRING", ()),
    ):
        assert handle_editor_script_message(
            "save_script",
            {"name": "[Doc] Report", "code": "e1", "origin": "document"},
            session_doc=doc,
            session_doc_url=None,
            send=sent.append,
        )
        msg = sent[-1]
        readonly_sentence = "Document is read-only or properties cannot be written."
        assert msg["status_error_text"] == readonly_sentence
        assert "status_ok_text" not in msg
        assert msg.get("selected_script_name") != "Report"

        selected_err = save_selected_script(doc, "[Doc] Report", "e2")
        assert selected_err == readonly_sentence

        assert get_document_scripts(doc) == {"Report": "old"}
        assert config_store["saved_python_scripts"] == {"Report": "keep-me"}


def test_save_script_to_origin_routes_by_origin():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.isReadonly = MagicMock(return_value=False)

    store: dict[str, Any] = {"saved_python_scripts": {}}

    def _get_config(key: str, default=None):
        if key == "saved_python_scripts":
            return dict(store["saved_python_scripts"])
        return default

    def _save(patch_dict, *, event_key=None):
        store.update(patch_dict)

    with (
        patch("plugin.framework.config.get_config", side_effect=_get_config),
        patch("plugin.framework.config.save", side_effect=_save),
    ):
        assert save_script_to_origin(doc, "doc_s", SCRIPT_ORIGIN_DOCUMENT, "x = 1") is None
        assert get_document_scripts(doc) == {"doc_s": "x = 1"}

        assert save_script_to_origin(doc, "user_empty", "", "y = 2") is None
        assert store["saved_python_scripts"]["user_empty"] == "y = 2"

        assert save_script_to_origin(doc, "user_explicit", SCRIPT_ORIGIN_USER, "z = 3") is None
        assert store["saved_python_scripts"]["user_explicit"] == "z = 3"

        doc.isReadonly.return_value = True
        err = save_script_to_origin(doc, "doc_s", SCRIPT_ORIGIN_DOCUMENT, "x = 99")
        assert isinstance(err, DocumentScriptError) and err.code == DocumentScriptErrorCode.READONLY
        assert get_document_scripts(doc) == {"doc_s": "x = 1"}
        assert "doc_s" not in store["saved_python_scripts"]


def test_delete_script_from_origin_no_document():
    err = delete_script_from_origin(None, "A", SCRIPT_ORIGIN_DOCUMENT)
    assert err is not None
    assert str(err) == "No document is open."
    assert err.code == DocumentScriptErrorCode.NO_DOCUMENT


def test_picker_delete_document_script_success_text():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.isReadonly = MagicMock(return_value=False)
    assert set_document_scripts(doc, {"A": "x"}) is None

    sent: list = []
    assert handle_editor_script_message(
        "delete_script",
        {"name": "[Doc] A", "origin": "document"},
        session_doc=doc,
        session_doc_url=None,
        send=sent.append,
    )
    assert sent[-1]["type"] == "scripts_list"
    assert sent[-1].get("status_ok_text")
    assert get_document_scripts(doc) == {}


def test_attach_script_stores_display_name_as_property_key():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    sent: list = []
    with patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""), patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        assert handle_editor_script_message(
            "save_script",
            {"name": "[Doc] Regional", "code": "doc-code", "origin": "document"},
            session_doc=doc,
            session_doc_url=None,
            send=sent.append,
        )
    stored = get_document_scripts(doc)
    assert stored["Regional"] == "doc-code"
    assert "[Doc] Regional" not in stored
    assert sent[-1]["status_ok_text"]


def test_handle_editor_script_message_disposed_document_returns_list_error():
    """Closing the document mid-save must not raise out of the picker handler."""
    from plugin.framework.errors import DocumentDisposedError

    sent: list = []
    with patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""), patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ), patch(
        "plugin.scripting.document_scripts.save_document_script",
        side_effect=DocumentDisposedError("gone"),
    ):
        assert (
            handle_editor_script_message(
                "save_script",
                {"name": "A", "code": "x = 1", "origin": "document"},
                session_doc=MagicMock(),
                session_doc_url="",
                send=sent.append,
            )
            is True
        )
    assert sent[-1]["type"] == "scripts_list"
    assert "closed" in sent[-1]["status_error_text"].lower()


def test_scripts_list_without_session_doc_is_not_writable():
    with (
        patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""),
        patch("plugin.scripting.domain_registry.PICKER_WIRING", ()),
    ):
        msg = build_scripts_list_message(
            session_doc=None,
            session_doc_url=None,
        )
    assert msg["document_available"] is False
    assert msg["document_readonly"] is True
    assert msg["document_stale"] is False
    doc_section = [s for s in msg["sections"] if s["id"] == "document"][0]
    assert doc_section["scripts"] == {}

    sent: list = []
    with (
        patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""),
        patch("plugin.scripting.document_scripts.save_document_script") as mock_save,
        patch("plugin.scripting.domain_registry.PICKER_WIRING", ()),
    ):
        handle_editor_script_message(
            "save_script",
            {"name": "A", "code": "x", "origin": "document"},
            session_doc=None,
            session_doc_url=None,
            send=sent.append,
        )
    assert "No document is open" in sent[-1]["status_error_text"]
    mock_save.assert_not_called()


def test_save_script_reserved_prefix_is_status_error():
    sent: list = []
    with (
        patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""),
        patch("plugin.framework.config.save") as mock_set,
        patch("plugin.scripting.domain_registry.PICKER_WIRING", ()),
    ):
        handled = handle_editor_script_message(
            "save_script",
            {"name": "[Doc] foo", "code": "print(1)", "origin": "user"},
            session_doc=None,
            session_doc_url=None,
            send=sent.append,
        )
    assert handled is True
    assert "reserved prefix" in sent[-1]["status_error_text"]
    for call in mock_set.call_args_list:
        assert "saved_python_scripts" not in call[0][0]

    err1 = save_user_script("[Doc] my_script", "code")
    assert err1 is not None
    assert err1.code == DocumentScriptErrorCode.RESERVED_NAME

    err2 = save_user_script("[Analysis] describe", "code")
    assert err2 is not None
    assert err2.code == DocumentScriptErrorCode.RESERVED_NAME


def test_closed_document_scripts_list_keeps_user_scripts():
    with patch("plugin.scripting.document_scripts.get_user_scripts", return_value={"Keep": "x = 1"}):
        msg = _closed_document_scripts_list("The document was closed.")
    user_sec = [s for s in msg["sections"] if s["id"] == "user"][0]
    doc_sec = [s for s in msg["sections"] if s["id"] == "document"][0]
    assert user_sec["scripts"] == {"Keep": "x = 1"}
    assert doc_sec["scripts"] == {}
    assert msg["document_available"] is False


def test_second_disposal_sends_user_scripts():
    from plugin.framework.errors import DocumentDisposedError

    sent: list = []
    with (
        patch("plugin.scripting.document_scripts.get_user_scripts", return_value={"Keep": "x = 1"}),
        patch("plugin.scripting.document_scripts.save_document_script", side_effect=DocumentDisposedError("gone")),
        patch("plugin.scripting.document_scripts.build_scripts_list_message", side_effect=DocumentDisposedError("gone again")),
    ):
        handled = handle_editor_script_message(
            "save_script",
            {"name": "A", "code": "x = 1", "origin": "document"},
            session_doc=MagicMock(),
            session_doc_url="",
            send=sent.append,
        )
    assert handled is True
    assert len(sent) == 1
    user_sec = [s for s in sent[0]["sections"] if s["id"] == "user"][0]
    assert "Keep" in user_sec["scripts"]


def test_builtin_origin_delete_does_not_touch_user_scripts():
    sent: list[dict] = []
    with patch("plugin.scripting.document_scripts.delete_user_script") as mock_del:
        handle_editor_script_message(
            "delete_script",
            {"name": "Plot", "origin": "viz"},
            session_doc=MagicMock(),
            session_doc_url="file:///a",
            send=sent.append,
        )
    mock_del.assert_not_called()
    assert sent[-1]["status_error_text"]


def test_stale_document_save_does_not_write():
    sent: list[dict] = []
    with (
        patch("plugin.scripting.document_scripts.document_scripts_identity", return_value="file:///other"),
        patch("plugin.scripting.document_scripts.save_document_script") as mock_save,
    ):
        handle_editor_script_message(
            "save_script",
            {"name": "A", "code": "x = 1", "origin": "document"},
            session_doc=MagicMock(),
            session_doc_url="file:///original",
            send=sent.append,
        )
    mock_save.assert_not_called()
    assert sent[-1]["status_error_text"]


def test_init_script_omitted_from_picker():
    with patch(
        "plugin.scripting.document_scripts.get_document_scripts",
        return_value={"INIT": "x = 1", "Hello": "y = 1"},
    ), patch(
        "plugin.scripting.domain_registry.PICKER_WIRING",
        (),
    ):
        items, merged, _origins = build_xdl_script_picker_state(MagicMock(), {})
    assert document_script_display_name("Hello") in items
    assert document_script_display_name("INIT") not in items
    assert "x = 1" not in merged.values()


def test_document_script_reopen_after_select_and_save_uses_display_key():
    """List, selection, and save share the [Doc] key so a same-named user script is not overwritten."""
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/test.odt")
    attach_document_script(doc, "Regional", "doc-code")
    config: dict[str, object] = {
        "saved_python_scripts": {"Regional": "user-code"},
        "last_python_script_name_writer": "",
    }

    def _get_config(key: str):
        value = config.get(key)
        return value if value is not None else ""

    def _save(patch_dict: dict) -> None:
        config.update(patch_dict)

    with patch("plugin.framework.config.get_config", side_effect=_get_config), patch(
        "plugin.framework.config.save", side_effect=_save
    ), patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
        return_value="last_python_script_name_writer",
    ):
        listed = build_scripts_list_message(session_doc=doc, session_doc_url="file:///tmp/test.odt")
        sections = {section["id"]: section["scripts"] for section in listed["sections"]}
        assert sections["user"]["Regional"] == "user-code"
        assert sections["document"]["[Doc] Regional"] == "doc-code"
        assert "Regional" not in sections["document"]

        sent: list[dict] = []
        assert handle_editor_script_message(
            "select_script",
            {"name": "[Doc] Regional"},
            session_doc=doc,
            session_doc_url="file:///tmp/test.odt",
            send=sent.append,
        )
        reopened = build_scripts_list_message(session_doc=doc, session_doc_url="file:///tmp/test.odt")
        assert reopened["selected_script_name"] == "[Doc] Regional"
        re_sections = {section["id"]: section["scripts"] for section in reopened["sections"]}
        assert re_sections["document"][reopened["selected_script_name"]] == "doc-code"

        assert handle_editor_script_message(
            "save_script",
            {"name": "[Doc] Regional", "code": "doc-new", "origin": "document"},
            session_doc=doc,
            session_doc_url="file:///tmp/test.odt",
            send=sent.append,
        )
    stored = get_document_scripts(doc)
    assert stored["Regional"] == "doc-new"
    assert "[Doc] Regional" not in stored
    assert config["saved_python_scripts"] == {"Regional": "user-code"}
    assert config["last_python_script_name_writer"] == "[Doc] Regional"


def test_scripts_list_keys_document_by_display_name_and_user_by_raw_name():
    """Document entries must use [Doc] labels because the JS overwrite probe keys on them.

    Probing the raw name once missed existing document scripts; the JS side itself has no test.
    """
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/test.odt")
    attach_document_script(doc, "Regional", "result = 3")
    with (
        patch("plugin.framework.config.get_config", side_effect=lambda k: {"Regional": "user-code"} if k == "saved_python_scripts" else ""),
        patch(
            "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
            return_value="last_python_script_name_writer",
        ),
    ):
        msg = build_scripts_list_message(session_doc=doc, session_doc_url="file:///tmp/test.odt")
    by_id = {section["id"]: section["scripts"] for section in msg["sections"]}
    assert "Regional" not in by_id["document"]
    assert by_id["document"][document_script_display_name("Regional")] == "result = 3"
    assert by_id["user"]["Regional"] == "user-code"


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


def test_build_scripts_list_message_loads_once_when_the_document_is_current():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/test.odt")
    attach_document_script(doc, "Regional", "doc-code")

    import plugin.scripting.document_scripts as ds
    import plugin.scripting.domain_registry as dr

    real_load = ds._load_document_script_map
    real_domain = dr.domain_template_entries
    load_counts = 0
    domain_counts = 0

    def counting_load(d):
        nonlocal load_counts
        load_counts += 1
        return real_load(d)

    def counting_domain(d):
        nonlocal domain_counts
        domain_counts += 1
        return real_domain(d)

    with (
        patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""),
        patch("plugin.framework.config.save"),
        patch("plugin.scripting.document_scripts._load_document_script_map", side_effect=counting_load),
        patch("plugin.scripting.document_scripts.domain_template_entries", side_effect=counting_domain),
    ):
        msg = build_scripts_list_message(
            session_doc=doc,
            session_doc_url=None,
        )

    assert load_counts == 1
    assert domain_counts == 1

    with (
        patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""),
        patch("plugin.framework.config.save"),
    ):
        expected_selected, _, _ = resolve_run_script_selection(
            doc, build_xdl_script_picker_state(doc, get_user_scripts())
        )

    assert msg["selected_script_name"] == expected_selected


def test_stale_list_selection_ignores_templates_of_the_stale_document():
    domain = MagicMock()
    domain.origin = "viz"
    domain.display_prefix = "[Viz] "
    domain.title = "Viz"
    domain.supports = lambda d: d is not None
    domain.templates = lambda: {"Plot": "result = 1"}

    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/current.odt")

    with (
        patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""),
        patch("plugin.framework.config.save") as mock_set_config,
        patch("plugin.scripting.domain_registry.PICKER_WIRING", (domain,)),
    ):
        msg = build_scripts_list_message(
            session_doc=doc,
            session_doc_url="file:///tmp/stale.odt",
        )

    assert any(s["id"] == "viz" for s in msg["sections"])
    assert msg["selected_script_name"] == ""
    mock_set_config.assert_not_called()


def test_build_scripts_list_message_stale_doc_does_not_select_doc_script():
    """Bug 2: stale doc passes None to selection resolution so no [Doc] script is selected."""
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/opened.ods")
    attach_document_script(doc, "SecretDocScript", "secret = 1")
    with patch("plugin.framework.config.get_config", side_effect=lambda k: {"UserScript": "x = 1"} if k == "saved_python_scripts" else "[Doc] SecretDocScript"), patch(
        "plugin.framework.config.save"
    ) as mock_set, patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
        return_value="last_python_script_name_calc",
    ):
        # session_doc_url is different from doc.getURL(), marking document_stale = True
        msg = build_scripts_list_message(
            session_doc=doc,
            session_doc_url="file:///tmp/other.ods",
        )
    assert msg["document_stale"] is True
    assert msg["selected_script_name"] != "[Doc] SecretDocScript"
    assert not msg["selected_script_name"].startswith("[Doc] ")
    # Ensure save did not store "[Doc] SecretDocScript" against the stale doc
    for call in mock_set.call_args_list:
        assert call.args[0].get("last_python_script_name_calc") != "[Doc] SecretDocScript"


def test_save_script_reserved_name_init_does_not_fallback_to_my_scripts():
    """Bug 3: reserving name INIT returns an error to user and does not write to My Scripts."""
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/doc.ods")
    sent: list = []
    with patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""), patch(
        "plugin.framework.config.save"
    ) as mock_set, patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
        return_value="last_python_script_name_calc",
    ):
        handled = handle_editor_script_message(
            "save_script",
            {"name": "INIT", "code": "malicious = 1", "origin": "document"},
            session_doc=doc,
            session_doc_url="file:///tmp/doc.ods",
            send=sent.append,
        )
    assert handled is True
    assert len(sent) == 1
    assert "status_ok_text" not in sent[0]
    assert sent[0].get("status_error_text")
    # Must NOT have written to My Scripts (saved_python_scripts)
    user_writes = [c for c in mock_set.call_args_list if c.args and "saved_python_scripts" in c.args[0]]
    assert user_writes == []


def test_save_script_too_large_does_not_fallback_to_my_scripts():
    """Bug 3: a script exceeding document size limits does not fall back to My Scripts."""
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/doc.ods")
    sent: list = []
    huge_code = "x = 1\n" * 200_000
    with patch("plugin.framework.config.get_config", side_effect=lambda k: {} if k == "saved_python_scripts" else ""), patch(
        "plugin.framework.config.save"
    ) as mock_set, patch(
        "plugin.scripting.document_scripts.resolve_run_script_name_config_key",
        return_value="last_python_script_name_calc",
    ):
        handled = handle_editor_script_message(
            "save_script",
            {"name": "TooLarge", "code": huge_code, "origin": "document"},
            session_doc=doc,
            session_doc_url="file:///tmp/doc.ods",
            send=sent.append,
        )
    assert handled is True
    assert len(sent) == 1
    assert "status_ok_text" not in sent[0]
    assert sent[0].get("status_error_text")
    user_writes = [c for c in mock_set.call_args_list if c.args and "saved_python_scripts" in c.args[0]]
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
        "plugin.framework.config.save"
    ) as mock_set:
        err = delete_user_script("NonExistent")
    assert err is not None
    assert err.code == DocumentScriptErrorCode.NOT_FOUND
    mock_set.assert_not_called()


def test_picker_delete_missing_script_returns_error():
    """Bug 4: IPC message delete_script for non-existent script returns status_error_text."""
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    doc.getURL = MagicMock(return_value="file:///tmp/doc.ods")

    # Document delete missing
    sent_doc: list = []
    handle_editor_script_message(
        "delete_script",
        {"name": "NonExistentDoc", "origin": "document"},
        session_doc=doc,
        session_doc_url="file:///tmp/doc.ods",
        send=sent_doc.append,
    )
    assert "status_ok_text" not in sent_doc[-1]
    assert sent_doc[-1].get("status_error_text")

    # User delete missing
    sent_user: list = []
    with patch("plugin.framework.config.get_config", return_value={}):
        handle_editor_script_message(
            "delete_script",
            {"name": "NonExistentUser", "origin": "user"},
            session_doc=doc,
            session_doc_url="file:///tmp/doc.ods",
            send=sent_user.append,
        )
    assert "status_ok_text" not in sent_user[-1]
    assert "does not exist" in sent_user[-1].get("status_error_text", "")


def test_enumerate_calc_documents_stops_on_stuck_enum():
    from plugin.scripting.document_scripts import _enumerate_calc_documents
    from plugin.framework.uno_context import _ENUM_CAP

    enum = MagicMock()
    enum.hasMoreElements.return_value = True
    enum.nextElement.side_effect = RuntimeError("stuck")
    comps = MagicMock()
    comps.createEnumeration.return_value = enum
    desktop = MagicMock()
    desktop.getComponents.return_value = comps

    results = _enumerate_calc_documents(desktop)
    assert results == []
    assert enum.nextElement.call_count == _ENUM_CAP


def test_enumerate_calc_documents_cap_hides_calc_past_cap():
    from plugin.scripting.document_scripts import _enumerate_calc_documents

    non_calcs = [MagicMock(name=f"non_calc_{i}") for i in range(32)]
    calc = MagicMock(name="calc")
    calc.getURL.return_value = "file:///calc.ods"
    for m in non_calcs:
        m.getURL.return_value = "file:///other.ods"

    comps = _calc_component_enum(*(non_calcs + [calc]))
    desktop = MagicMock()
    desktop.getComponents.return_value = comps

    with patch("plugin.scripting.document_scripts.is_calc", side_effect=lambda m: m is calc):
        results = _enumerate_calc_documents(desktop)
    assert calc not in results

    further_non_calcs = [MagicMock(name=f"further_{i}") for i in range(40)]
    for m in further_non_calcs:
        m.getURL.return_value = "file:///other.ods"
    early_comps = _calc_component_enum(*([calc] + further_non_calcs))
    desktop.getComponents.return_value = early_comps

    with patch("plugin.scripting.document_scripts.is_calc", side_effect=lambda m: m is calc):
        results_early = _enumerate_calc_documents(desktop)
    assert results_early == [calc]


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
        "plugin.scripting.document_scripts.save_user_script",
        return_value=None,
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



