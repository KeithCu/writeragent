# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Shared-kernel session ids for Calc =PYTHON() and menubar reset."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from plugin.chatbot.dialogs import msgbox
from plugin.doc.document_helpers import get_document_property, is_calc, set_document_property
from plugin.framework.config import get_config_str
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_desktop
from plugin.scripting.venv_worker import reset_python_session

log = logging.getLogger(__name__)

PYTHON_WORKBOOK_SESSION_PROP = "WriterAgentPythonSessionId"
_SESSION_MODE_KEY = "scripting.python_session_mode"


def python_session_mode(ctx: Any) -> str:
    """Return ``isolated`` or ``shared`` from config (default ``isolated``)."""
    mode = (get_config_str(ctx, _SESSION_MODE_KEY) or "isolated").strip().lower()
    if mode == "shared":
        return "shared"
    return "isolated"


def _calc_document(ctx: Any) -> Any | None:
    try:
        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent()
    except Exception:
        log.debug("session_manager: could not get current component", exc_info=True)
        return None
    if doc is None or not is_calc(doc):
        return None
    return doc


def _workbook_session_key(doc: Any) -> str:
    url = ""
    try:
        url = (getattr(doc, "getURL", lambda: "")() or "").strip()
    except Exception:
        pass
    if url:
        return url
    existing = get_document_property(doc, PYTHON_WORKBOOK_SESSION_PROP)
    if existing:
        return str(existing)
    new_id = str(uuid.uuid4())
    set_document_property(doc, PYTHON_WORKBOOK_SESSION_PROP, new_id)
    return new_id


def workbook_session_id(ctx: Any) -> str | None:
    """Return ``calc:…`` session id when shared mode and active doc is Calc, else ``None``."""
    if python_session_mode(ctx) != "shared":
        return None
    doc = _calc_document(ctx)
    if doc is None:
        return None
    return f"calc:{_workbook_session_key(doc)}"


def reset_workbook_python_session(ctx: Any) -> None:
    """Menubar handler: reset shared Python namespace for the active Calc workbook."""
    if python_session_mode(ctx) != "shared":
        msgbox(
            ctx,
            "WriterAgent",
            _(
                "Python session mode is Isolated. Enable Shared kernel in Settings → Python "
                "to keep variables between =PYTHON() cells, then use Reset Python Session."
            ),
        )
        return

    doc = _calc_document(ctx)
    if doc is None:
        msgbox(
            ctx,
            "WriterAgent",
            _(
                "Reset Python Session applies to Calc spreadsheets with =PYTHON() in shared "
                "kernel mode. Open a Calc workbook and try again."
            ),
        )
        return

    session_id = f"calc:{_workbook_session_key(doc)}"
    res = reset_python_session(ctx, session_id)
    if res.get("status") == "ok":
        msgbox(ctx, "WriterAgent", _("Python session reset for this workbook."))
        return

    msg = res.get("message") or _("Could not reset Python session.")
    msgbox(ctx, "WriterAgent", _("Error: {0}").format(msg))
