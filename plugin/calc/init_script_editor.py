# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Monaco editor for per-workbook Calc initialization scripts."""

from __future__ import annotations

import logging
from typing import Any

# Optional: reset worker init/cell sessions on workbook close (see python_workbook_lifecycle.py).
# from plugin.calc.python_workbook_lifecycle import ensure_calc_workbook_unload_resets_python
from plugin.chatbot.dialogs import msgbox
from plugin.framework.i18n import _
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.editor_host import launch_monaco_editor, monaco_editor_available
from plugin.scripting.init_scripts import get_calc_document_from_ctx, get_calc_init_script, set_calc_init_script
from plugin.scripting.session_manager import calc_workbook_base_session_id
from plugin.scripting.venv_worker import reset_python_session, warm_venv_worker

log = logging.getLogger("writeragent.scripting")


def open_calc_init_script_editor(ctx: Any) -> None:
    """Menubar entry: edit the active Calc workbook's initialization script."""
    doc = get_calc_document_from_ctx(ctx)
    if doc is None:
        msgbox(
            ctx,
            "WriterAgent",
            _("Edit Initialization Script applies to Calc spreadsheets. Open a Calc workbook and try again."),
        )
        return

    # ensure_calc_workbook_unload_resets_python(ctx, doc)
    initial_code = get_calc_init_script(doc)
    exe, monaco_ok = monaco_editor_available(ctx)
    if not monaco_ok or not exe:
        msgbox(
            ctx,
            "WriterAgent",
            _(
                "The Python editor requires a configured venv with pywebview. "
                "Set Settings → Python → Python venv path and install pywebview in that venv."
            ),
        )
        return

    save_ok_text = _("Initialization script saved.")

    def on_save(code: str, _save_as_plain: bool, _data_binding: str | None = None, action: str = "save") -> dict[str, Any]:
        if action != "save":
            return {"type": "saved", "ok": True, "status_ok_text": save_ok_text}
        err = set_calc_init_script(doc, code)
        if err:
            return {"type": "error", "message": err}
        # Drop init + shared cell sessions so the new script runs on next =PYTHON().
        reset_python_session(ctx, calc_workbook_base_session_id(doc))
        return {"type": "saved", "ok": True, "status_ok_text": save_ok_text}

    load_msg: dict[str, Any] = {
        "type": "load",
        "mode": "run_script",
        "language": "python",
        "code": initial_code,
        "title": _("Workbook Initialization Script"),
        "run_label": _("Save"),
        "save_label": _("Save"),
        "close_label": _("Close"),
        "show_plain_text": False,
        "show_data_binding": False,
        "status_ok_text": save_ok_text,
        "saved_ok_text": save_ok_text,
    }

    run_in_background(warm_venv_worker, ctx, name="warm-venv-worker")
    if not launch_monaco_editor(ctx, exe=exe, load_message=load_msg, on_save=on_save):
        msgbox(ctx, "WriterAgent", _("Could not open the Python editor."))
