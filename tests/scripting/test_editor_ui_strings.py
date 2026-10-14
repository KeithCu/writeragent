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
)
import pytest


@pytest.mark.parametrize(
    "mode, value, value_2",
    [
        pytest.param("init_script", "Save", "Initialization script saved.", id="test_init_script_ui_uses_initialization_strings"),
        pytest.param("latex", "Insert", "Formula inserted.", id="test_latex_ui_uses_insert_label"),
    ],
)
def test_init_script_ui_uses_initialization_strings(mode, value, value_2):
    ui = build_monaco_ui_strings(mode=mode)
    assert ui["save_label"] == _(value)
    assert ui["saved_default"] == _(value_2)


def test_calc_cell_ui_includes_data_binding_and_cancel():
    ui = build_monaco_ui_strings(mode="calc_cell")
    assert ui["close_label"] == ui["cancel_label"]
    assert ui["data_label"] == _("Data:")
    assert ui["data_binding_title"]
    assert ui["saved_plain"] == _("Saved without =PY().")
    assert ui["jedi_missing"]


def test_run_script_ui_matches_native_dialog_labels():
    ui = build_monaco_ui_strings(mode="run_script")
    assert ui["new_label"] == _("New")
    assert ui["new_script_title"] == _("New Python Script")
    assert ui["script_label"] == _("Script:")
    assert ui["save_as_label"] == _("Save As...")
    assert ui["save_as_title"] == _("Save Script As")
    assert ui["delete_label"] == _("Delete")
    assert ui["builtin_readonly"] == _("Built-in helpers are read-only. Use Save As to copy one into My Scripts.")
    assert "copying_script" not in ui
    assert "copy_prompt" not in ui

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
    assert "title" not in msg["ui"]
    assert msg["ui"]["save_label"] == _("Save")
    assert msg["ui"]["data_binding_title"] == _("Custom tooltip")
    assert msg["title"] == _("Python cell editor")


def test_enrich_leaves_status_ok_text_at_top_level():
    msg = enrich_monaco_load_message(
        {
            "type": "load",
            "mode": "calc_cell",
            "title": "Python cell editor",
            "status_ok_text": "Saved code.",
        }
    )
    assert msg["title"] == "Python cell editor"
    assert msg["status_ok_text"] == "Saved code."
    assert "title" not in msg["ui"]
    assert "status_ok_text" not in msg["ui"]


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
    required = ("status_prefix", "ready", "error", "saving", "running", "save_label", "close_label", "jedi_missing")
    for mode in ("calc_cell", "run_script", "latex", "init_script"):
        ui = build_monaco_ui_strings(mode=mode)
        for key in required:
            assert ui.get(key), f"missing {key} for mode {mode}"


def test_unknown_mode_uses_calc_cell_catalog():
    assert build_monaco_ui_strings(mode="not-a-mode") == build_monaco_ui_strings(mode="calc_cell")
