# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Localized Monaco editor shell strings (Python gettext → IPC ``load.ui``)."""

# =========================================================================================
# WARNING: PARITY INVARIANT WITH MONACO JAVASCRIPT FRONTEND
# If you add, rename, or change string keys in this file, you MUST also update:
#   - JS Script Manager:        plugin/contrib/scripting/assets/editor/scripts_manager.js
#   - Monaco Editor Script:     plugin/contrib/scripting/assets/editor/editor_shell.js
#   - Monaco HTML / Toolbar:    plugin/contrib/scripting/assets/editor/index.html
#   - Native Cell Editor:       plugin/calc/python/cell_editor_ui.py
#   - Native Cell Dialog:       extension/Dialogs/PythonCellEditorDialog.xdl
# calc_cell msgids (Save without =PY(), Data:, status) must stay identical in native UI.
# Plain-save status “Saved without =PY().” is status_ok_text on the saved frame
# (editor.py) and the native fallback (cell_editor_ui.py). Do not put it back in
# this catalog; editor_shell.js does not read ui.saved_plain.
# =========================================================================================

from __future__ import annotations

from typing import Any

from plugin.framework.i18n import _

# Top-level load keys copied into ui. title, status_ok_text, and saved_ok_text
# stay top-level; editor_shell.js resolves the saved status itself.
_UI_OVERRIDE_KEYS: tuple[str, ...] = (
    "save_label",
    "run_label",
    "close_label",
    "plain_text_label",
    "data_binding_title",
)


def _shared_ui_strings() -> dict[str, str]:
    return {
        "status_prefix": _("Status:"),
        "ready": _("Ready"),
        "error": _("Error"),
        "saving": _("Saving…"),
        "running": _("Running…"),
        "monaco_loader_missing": _("Monaco loader missing."),
        "jedi_missing": _("Install jedi in the Python venv for completions."),
        "run_label": _("Run"),
        "save_label": _("Save"),
        "close_label": _("Close"),
        "cancel_label": _("Cancel"),
    }


def _data_strings() -> dict[str, str]:
    return {
        "data_label": _("Data:"),
        "data_placeholder": _("A1:C1  or  A1:C1, C1:C5"),
    }


def _calc_cell_ui_strings() -> dict[str, str]:
    ui = _shared_ui_strings()
    ui["close_label"] = ui["cancel_label"]
    ui.update(_data_strings())
    ui.update(
        {
            "plain_text_label": _("Save without =PY()"),
            "data_binding_title": _(
                "Calc injects `data` and `ranges` from these range(s) at runtime."
            ),
            "data_binding_disabled_title": _(
                "Data ranges apply only when saving as a =PY() formula."
            ),
            "saved_default": _("Saved."),
        }
    )
    return ui


def _run_script_ui_strings() -> dict[str, str]:
    ui = _shared_ui_strings()
    ui.update(_data_strings())
    ui.update(
        {
            "new_label": _("New"),
            "new_script_title": _("New Python Script"),
            "script_name_label": _("Script name:"),
            "attach_to_document_label": _("Attach to this document"),
            "attach_unavailable_no_document": _(
                "Attach to this document (no document is open)"
            ),
            "attach_unavailable_readonly": _(
                "Attach to this document (this document is read-only)"
            ),
            "attach_unavailable_stale": _(
                "Attach to this document (this document changed)"
            ),
            "create_label": _("Create"),
            "script_name_required": _("Script name cannot be empty."),
            "script_label": _("Script:"),
            "save_as_label": _("Save As..."),
            "save_as_title": _("Save Script As"),
            "delete_label": _("Delete"),
            "scripts_fallback": _("Scripts"),
            "my_scripts_fallback": _("My Scripts"),
            "builtin_readonly": _(
                "Built-in helpers are read-only. Use Save As to copy one into My Scripts."
            ),
            "document_stale": _(
                "Document changed — close and reopen Run Python Script to edit document scripts."
            ),
            "loaded_script": _("Loaded script '{0}'."),
            "saving_script": _("Saving script '{0}'..."),
            "deleting_script": _("Deleting script '{0}'..."),
            "builtin_cannot_delete": _("Built-in helpers cannot be deleted."),
            "attach_overwrite_confirm": _(
                "A script named '{0}' already exists in this document. Overwrite?"
            ),
            "copy_overwrite_confirm": _("A script named '{0}' already exists in My Scripts. Overwrite?"),
            "delete_confirm": _("Are you sure you want to delete script '{0}'?"),
            "data_binding_title": _("Select data range or enter A1 address (injected as data)."),
        }
    )
    return ui


def _latex_ui_strings() -> dict[str, str]:
    ui = _shared_ui_strings()
    ui.update(
        {
            "plain_text_label": _("Insert as display block (centered paragraph)"),
            "save_label": _("Insert"),
            "close_label": _("Close"),
            "saved_default": _("Formula inserted."),
        }
    )
    return ui


def _init_script_ui_strings() -> dict[str, str]:
    ui = _shared_ui_strings()
    ui.update(
        {
            "save_label": _("Save"),
            "close_label": _("Close"),
            "saved_default": _("Initialization script saved."),
        }
    )
    return ui


def build_monaco_ui_strings(*, mode: str) -> dict[str, str]:
    """Return static Monaco shell strings for one editor mode.

    ``run_script``, ``latex``, and ``init_script`` have their own catalogs.
    Any other mode, including ``calc_cell``, uses the calc_cell catalog.
    A bad mode still gets labels.
    """
    if mode == "run_script":
        return _run_script_ui_strings()
    if mode == "latex":
        return _latex_ui_strings()
    if mode == "init_script":
        return _init_script_ui_strings()
    return _calc_cell_ui_strings()


def enrich_monaco_load_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Attach ``ui`` dict and merge caller overrides from top-level load keys."""
    enriched = dict(msg)
    # Always a bool: a reused Monaco window kept the previous mode's plain-save checkbox.
    enriched["save_as_plain"] = bool(enriched.get("save_as_plain", False))
    mode = str(enriched.get("mode") or "calc_cell")
    ui = build_monaco_ui_strings(mode=mode)

    for key in _UI_OVERRIDE_KEYS:
        value = enriched.get(key)
        if isinstance(value, str) and value:
            ui[key] = value

    enriched["ui"] = ui
    return enriched
