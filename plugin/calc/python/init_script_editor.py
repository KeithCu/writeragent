# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Open Monaco to edit the Calc workbook initialization script (INIT)."""

from __future__ import annotations

from typing import Any

from plugin.chatbot.dialogs import msgbox, msgbox_with_report
from plugin.framework.config import get_config
from plugin.framework.errors import is_disposed_exception
from plugin.framework.i18n import _
from plugin.framework.thread_guard import main_thread_only
from plugin.scripting.document_scripts import (
    document_scripts_identity,
    get_calc_document_from_ctx,
    get_calc_init_script,
    set_calc_init_script,
)
from plugin.scripting.editor_errors import failure_message
from plugin.scripting.editor_host import launch_monaco_editor, monaco_editor_available, probe_webview_import, resolve_editor_python


@main_thread_only
def open_init_script_editor(ctx: Any) -> None:
    """Open Monaco for the active Calc workbook's initialization script."""
    title = _("Edit Initialization Script")
    doc = get_calc_document_from_ctx(ctx)
    if doc is None:
        msgbox(ctx, title, _("Open a Calc spreadsheet first."), box_type=1)
        return

    exe, monaco_available = monaco_editor_available(ctx)
    if not monaco_available:
        if get_config("scripting.force_internal_script_editor"):
            msgbox(ctx, title, _('The Monaco editor is disabled by "scripting.force_internal_script_editor" in writeragent.json.'), box_type=3)
            return
        if not exe:
            _unused, err = resolve_editor_python(ctx)
            msgbox(ctx, title, err or _("Configure a Python venv path in Settings → Python."), box_type=3)
            return
        webview_ok, webview_detail = probe_webview_import(exe)
        if not webview_ok:
            msg = failure_message(_("Cannot import webview (pywebview) in the configured venv."), detail=webview_detail)
            msgbox_with_report(ctx, title, msg, box_type=3, reportable=True)
            return

    assert exe is not None
    closed_message = _("The spreadsheet is no longer open. Reopen it and try again.")
    try:
        initial = get_calc_init_script(doc)
    except Exception as exc:
        if not is_disposed_exception(exc):
            raise
        msgbox(ctx, title, closed_message, box_type=3)
        return

    def on_save(code: str, _save_as_plain: bool, _data_binding: str | None, action: str) -> dict[str, Any]:
        nonlocal initial
        new_code = code or ""
        if new_code == initial:
            return {"type": "saved", "ok": True, "status_ok_text": _("Initialization script saved.")}
        try:
            err = set_calc_init_script(doc, new_code)
        except Exception as exc:
            if not is_disposed_exception(exc):
                raise
            return {"type": "error", "message": closed_message}
        if err is not None:
            return {"type": "error", "message": err}
        initial = new_code
        from plugin.calc.python.function import clear_python_addin_cache

        clear_python_addin_cache()
        return {"type": "saved", "ok": True, "status_ok_text": _("Initialization script saved.")}

    load_msg: dict[str, Any] = {
        "type": "load",
        "mode": "init_script",
        "language": "python",
        "code": initial,
        "title": title,
        "save_as_plain": True,
        "plain_text_label": _("Save initialization script"),
        "resource": "init",
        "doc_url": document_scripts_identity(doc),
        "show_data_binding": False,
    }
    launch_monaco_editor(ctx, exe=exe, load_message=load_msg, on_save=on_save)
