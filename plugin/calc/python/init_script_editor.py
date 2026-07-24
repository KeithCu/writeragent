# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Open Monaco to edit the Calc workbook initialization script (INIT)."""

from __future__ import annotations

import logging
from typing import Any

from plugin.chatbot.dialogs import msgbox, msgbox_with_report
from plugin.framework.config import get_config
from plugin.framework.i18n import _
from plugin.scripting.document_scripts import (
    get_calc_document_from_ctx,
    get_calc_init_script,
    set_calc_init_script,
)
from plugin.scripting.editor_host import (
    launch_monaco_editor,
    monaco_editor_available,
    probe_webview_import,
    resolve_editor_python,
)
from plugin.scripting.editor_ipc import failure_message
from plugin.scripting.session_manager import reset_workbook_python_session

log = logging.getLogger(__name__)


def open_init_script_editor(ctx: Any = None) -> bool:
    """Open Monaco for the active Calc workbook's initialization script.

    Returns True when the editor launched successfully.
    """
    from plugin.framework.uno_context import get_ctx

    uno_ctx = ctx or get_ctx()
    doc = get_calc_document_from_ctx(uno_ctx)
    if doc is None:
        msgbox(uno_ctx, _("Edit Initialization Script"), _("Open a Calc spreadsheet first."), box_type=1)
        return False

    exe, monaco_available = monaco_editor_available(uno_ctx)
    if not monaco_available:
        if get_config("scripting.force_internal_script_editor"):
            msgbox(
                uno_ctx,
                _("Edit Initialization Script"),
                _(
                    "The Monaco editor is disabled by "
                    '"scripting.force_internal_script_editor" in writeragent.json.'
                ),
                box_type=3,
            )
            return False
        if not exe:
            unused, err = resolve_editor_python(uno_ctx)
            msgbox(
                uno_ctx,
                _("Edit Initialization Script"),
                err or _("Configure a Python venv path in Settings → Python."),
                box_type=3,
            )
            return False
        webview_ok, webview_detail = probe_webview_import(exe)
        if not webview_ok:
            msg = failure_message(
                _("Cannot import webview (pywebview) in the configured venv."),
                detail=webview_detail,
            )
            msgbox_with_report(
                uno_ctx,
                _("Edit Initialization Script"),
                msg,
                box_type=3,
                reportable=True,
            )
            return False

    if not exe:
        unused, err = resolve_editor_python(uno_ctx)
        msgbox(
            uno_ctx,
            _("Edit Initialization Script"),
            err or _("Configure a Python venv path in Settings → Python."),
            box_type=3,
        )
        return False

    initial = get_calc_init_script(doc) or ""

    def on_save(code: str, _save_as_plain: bool, _data_binding: str | None, action: str) -> dict[str, Any]:
        err = set_calc_init_script(doc, code or "")
        if err:
            return {"type": "error", "message": err}
        # Init script hash change should re-seed; reset so next =PY() picks up new INIT.
        try:
            reset_workbook_python_session(uno_ctx)
        except Exception:
            log.debug("init_script_editor: reset after save failed", exc_info=True)
        return {"type": "saved", "ok": True, "status_ok_text": _("Initialization script saved.")}

    load_msg: dict[str, Any] = {
        "type": "load",
        "mode": "init_script",
        "language": "python",
        "code": initial,
        "title": _("Edit Initialization Script"),
        "save_as_plain": True,
        "plain_text_label": _("Save initialization script"),
    }
    return bool(launch_monaco_editor(uno_ctx, exe=exe, load_message=load_msg, on_save=on_save))
