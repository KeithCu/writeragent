# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Monaco editor UI string catalog and load enrichment."""

from __future__ import annotations

from plugin.framework.i18n import _
from plugin.scripting.editor_ui_strings import (
    build_monaco_ui_strings,
    enrich_monaco_load_message,
    format_js,
)


def test_format_js_uses_numeric_placeholders():
    assert format_js("Loaded script '{0}'.", "foo") == "Loaded script 'foo'."


def test_calc_cell_ui_includes_data_binding_and_cancel():
    ui = build_monaco_ui_strings(mode="calc_cell")
    assert ui["close_label"] == ui["cancel_label"]
    assert ui["data_label"] == _("Data:")
    assert ui["data_binding_title"]
    assert ui["saved_plain"] == _("Saved without =PY().")


def test_run_script_ui_matches_native_dialog_labels():
    ui = build_monaco_ui_strings(mode="run_script")
    assert ui["script_label"] == _("Script:")
    assert ui["attach_label"] == _("Attach")
    assert ui["save_as_label"] == _("Save As...")
    assert ui["delete_label"] == _("Delete")
    assert ui["sample_label"] == _("Sample")
    assert ui["attach_prompt"] == _("Enter script name:")


def test_latex_ui_uses_insert_label():
    ui = build_monaco_ui_strings(mode="latex")
    assert ui["save_label"] == _("Insert")
    assert ui["saved_default"] == _("Formula inserted.")


def test_enrich_monaco_load_message_adds_ui_and_merges_overrides():
    msg = enrich_monaco_load_message(
        {
            "type": "load",
            "mode": "calc_cell",
            "title": _("Python cell editor"),
            "save_label": _("Save"),
            "data_binding_title": _("Custom tooltip"),
        }
    )
    assert "ui" in msg
    assert msg["ui"]["title"] == _("Python cell editor")
    assert msg["ui"]["save_label"] == _("Save")
    assert msg["ui"]["data_binding_title"] == _("Custom tooltip")
    assert msg["title"] == _("Python cell editor")


def test_enrich_maps_saved_ok_text_to_saved_default():
    msg = enrich_monaco_load_message(
        {
            "type": "load",
            "mode": "run_script",
            "saved_ok_text": _("Script saved."),
        }
    )
    assert msg["ui"]["saved_default"] == _("Script saved.")


def test_all_modes_return_non_empty_required_keys():
    required = ("status_prefix", "ready", "error", "saving", "running", "save_label", "close_label")
    for mode in ("calc_cell", "run_script", "latex"):
        ui = build_monaco_ui_strings(mode=mode)
        for key in required:
            assert ui.get(key), f"missing {key} for mode {mode}"
