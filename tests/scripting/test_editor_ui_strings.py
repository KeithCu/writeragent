# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Monaco editor UI string catalog and load enrichment."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from plugin.framework.i18n import _
from plugin.scripting.editor_ui_strings import (
    build_monaco_ui_strings,
    enrich_monaco_load_message,
)

# Keys read in JS but not expected in the UI catalog. Stays empty because
# saved_ok_text and status_ok_text are msg fields.
_JS_UI_KEYS_NOT_IN_CATALOG: frozenset[str] = frozenset()


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
    assert "saved_plain" not in ui
    assert ui["saved_default"] == _("Saved.")
    assert ui["jedi_missing"]
    assert ui["editor_loader_missing"] == _("Editor loader missing.")


def test_run_script_ui_matches_native_dialog_labels():
    ui = build_monaco_ui_strings(mode="run_script")
    assert ui["new_label"] == _("New")
    assert ui["new_script_title"] == _("New Python Script")
    assert ui["script_label"] == _("Script:")
    assert ui["save_as_label"] == _("Save As...")
    assert ui["save_as_title"] == _("Save Script As")
    assert ui["delete_label"] == _("Delete")
    assert ui["attach_to_document_label"] == _("Attach to this document")
    assert ui["attach_unavailable_no_document"] == _(
        "Attach to this document (no document is open)"
    )
    assert ui["attach_unavailable_readonly"] == _(
        "Attach to this document (this document is read-only)"
    )
    assert ui["attach_unavailable_stale"] == _(
        "Attach to this document (this document changed)"
    )
    assert ui["builtin_readonly"] == _("Built-in helpers are read-only. Use Save As to copy one into My Scripts.")
    assert ui["unsaved_changes_confirm"] == _("Discard unsaved changes to '{0}'?")
    assert ui["this_script"] == _("this script")
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


def test_enrich_does_not_copy_saved_ok_text_into_ui():
    msg = enrich_monaco_load_message(
        {
            "type": "load",
            "mode": "calc_cell",
            "saved_ok_text": "Custom",
        }
    )
    assert msg["saved_ok_text"] == "Custom"
    assert msg["ui"]["saved_default"] == _("Saved.")
    assert "saved_ok_text" not in msg["ui"]


def test_js_t_and_fmt_keys_are_in_the_ui_catalog():
    repo_root = Path(__file__).resolve().parents[2]
    editor_js = (repo_root / "plugin/contrib/scripting/assets/editor/editor_shell.js").read_text(encoding="utf-8")
    scripts_manager_js = (repo_root / "plugin/contrib/scripting/assets/editor/scripts_manager.js").read_text(encoding="utf-8")

    direct_pattern = re.compile(r"""(?<![.\w])(?:t|fmt)\(\s*["']([A-Za-z0-9_]+)["']""")
    bare_fmt_pattern = re.compile(r"""(?<!function )(?<![.\w])fmt\(\s*([A-Za-z_][A-Za-z0-9_]*)""")

    editor_bare = set(bare_fmt_pattern.findall(editor_js))
    scripts_manager_bare = set(bare_fmt_pattern.findall(scripts_manager_js))
    assert not editor_bare
    assert scripts_manager_bare == {"msgKey"}

    assert '"attach_overwrite_confirm"' in scripts_manager_js
    assert '"copy_overwrite_confirm"' in scripts_manager_js

    keys = set(direct_pattern.findall(editor_js)) | set(direct_pattern.findall(scripts_manager_js))
    keys.update({"attach_overwrite_confirm", "copy_overwrite_confirm"})

    catalog: set[str] = set()
    for mode in ("calc_cell", "run_script", "latex", "init_script"):
        catalog.update(build_monaco_ui_strings(mode=mode))

    assert keys <= catalog | _JS_UI_KEYS_NOT_IN_CATALOG
    assert "=PYTHON()" not in editor_js


def test_enrich_always_sends_a_boolean_save_as_plain():
    run = enrich_monaco_load_message({"type": "load", "mode": "run_script"})
    assert run["save_as_plain"] is False
    cell = enrich_monaco_load_message({"type": "load", "mode": "calc_cell", "save_as_plain": True})
    assert cell["save_as_plain"] is True


def test_all_modes_return_non_empty_required_keys():
    required = ("status_prefix", "ready", "error", "saving", "running", "save_label", "close_label", "jedi_missing")
    for mode in ("calc_cell", "run_script", "latex", "init_script"):
        ui = build_monaco_ui_strings(mode=mode)
        for key in required:
            assert ui.get(key), f"missing {key} for mode {mode}"


def test_unknown_mode_uses_calc_cell_catalog():
    assert build_monaco_ui_strings(mode="not-a-mode") == build_monaco_ui_strings(mode="calc_cell")
