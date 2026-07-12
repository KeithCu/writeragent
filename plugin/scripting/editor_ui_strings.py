# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Localized Monaco editor shell strings (Python gettext → IPC ``load.ui``)."""

from __future__ import annotations

from typing import Any

from plugin.framework.i18n import _

# Top-level load keys copied into ``ui`` when present (caller overrides win).
_LOAD_OVERRIDE_KEYS = (
    "title",
    "save_label",
    "run_label",
    "close_label",
    "plain_text_label",
    "saved_ok_text",
    "status_ok_text",
    "data_binding_title",
)


def format_js(template: str, *args: Any) -> str:
    """Format a template for JS ``fmt()`` — uses ``{0}`` placeholders like Python ``.format()``."""
    return template.format(*args)


def _shared_ui_strings() -> dict[str, str]:
    return {
        "status_prefix": _("Status:"),
        "ready": _("Ready"),
        "error": _("Error"),
        "saving": _("Saving…"),
        "running": _("Running…"),
        "monaco_loader_missing": _("Monaco loader missing."),
        "run_label": _("Run"),
        "save_label": _("Save"),
        "close_label": _("Close"),
        "cancel_label": _("Cancel"),
    }


def _calc_cell_ui_strings() -> dict[str, str]:
    ui = _shared_ui_strings()
    ui["close_label"] = ui["cancel_label"]
    ui.update(
        {
            "plain_text_label": _("Save without =PY()"),
            "data_label": _("Data:"),
            "data_placeholder": _("A1:C1  or  A1:C1, C1:C5"),
            "data_binding_title": _(
                "Calc injects `data` and `data_list` from these range(s) at runtime."
            ),
            "data_binding_disabled_title": _(
                "Data ranges apply only when saving as a =PY() formula."
            ),
            "saved_default": _("Saved."),
            "saved_plain": _("Saved without =PY()."),
        }
    )
    return ui


def _run_script_ui_strings() -> dict[str, str]:
    ui = _shared_ui_strings()
    ui.update(
        {
            "script_label": _("Script:"),
            "sample_label": _("Sample"),
            "attach_label": _("Attach"),
            "attach_title": _("Attach to Document"),
            "save_as_label": _("Save As..."),
            "copy_to_user_label": _("Copy to My Scripts"),
            "copy_to_user_title": _("Copy to My Scripts"),
            "delete_label": _("Delete"),
            "scripts_fallback": _("Scripts"),
            "my_scripts_fallback": _("My Scripts"),
            "builtin_readonly": _(
                "Built-in helpers are read-only. Use Copy to My Scripts to customize."
            ),
            "document_stale": _(
                "Document changed — close and reopen Run Python Script to edit document scripts."
            ),
            "loaded_script": _("Loaded script '{0}'."),
            "loaded_sample": _("Loaded Sample scratchpad."),
            "cannot_attach": _("No document is open to attach scripts."),
            "attaching_script": _("Attaching script '{0}'..."),
            "copying_script": _("Copying script '{0}' to My Scripts..."),
            "saving_script": _("Saving script '{0}'..."),
            "deleting_script": _("Deleting script '{0}'..."),
            "builtin_cannot_delete": _("Built-in helpers cannot be deleted."),
            "cleared_sample": _("Cleared Sample scratchpad."),
            "attach_prompt": _("Enter script name:"),
            "attach_overwrite_confirm": _(
                "A script named '{0}' already exists in this document. Overwrite?"
            ),
            "copy_prompt": _("Copy to My Scripts as:"),
            "copy_overwrite_confirm": _("A script named '{0}' already exists in My Scripts. Overwrite?"),
            "save_as_prompt": _("Enter script name:"),
            "save_to_document_confirm": _("Save script '{0}' to this document?"),
            "clear_sample_confirm": _("Are you sure you want to clear the Sample scratchpad?"),
            "delete_confirm": _("Are you sure you want to delete script '{0}'?"),
            "data_binding_title": _("Select data range or enter A1 address (injected as data)."),
            "data_label": _("Data:"),
            "data_placeholder": _("A1:C1  or  A1:C1, C1:C5"),
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


def build_monaco_ui_strings(*, mode: str) -> dict[str, str]:
    """Return static Monaco shell strings for *calc_cell*, *run_script*, or *latex*."""
    if mode == "run_script":
        return _run_script_ui_strings()
    if mode == "latex":
        return _latex_ui_strings()
    return _calc_cell_ui_strings()


def enrich_monaco_load_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Attach ``ui`` dict and merge caller overrides from top-level load keys."""
    enriched = dict(msg)
    mode = str(enriched.get("mode") or "calc_cell")
    ui = build_monaco_ui_strings(mode=mode)

    for key in _LOAD_OVERRIDE_KEYS:
        value = enriched.get(key)
        if isinstance(value, str) and value:
            ui_key = key
            if key == "saved_ok_text":
                ui_key = "saved_default"
            ui[ui_key] = value

    if mode == "calc_cell" and "close_label" not in enriched:
        ui["close_label"] = ui["cancel_label"]

    enriched["ui"] = ui
    return enriched
