# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Shared-kernel session ids for Calc =PYTHON(), Writer notebooks, and menubar reset."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from plugin.chatbot.dialogs import msgbox
from plugin.doc.document_helpers import get_document_property, is_calc, is_writer, set_document_property
from plugin.framework.config import get_config_str
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_desktop
from plugin.notebook.cell_registry import has_notebook_registry
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


def _active_document(ctx: Any) -> Any | None:
    try:
        desktop = get_desktop(ctx)
        return desktop.getCurrentComponent()
    except Exception:
        log.debug("session_manager: could not get current component", exc_info=True)
        return None


def _calc_document(ctx: Any) -> Any | None:
    doc = _active_document(ctx)
    if doc is None or not is_calc(doc):
        return None
    return doc


def _writer_document(ctx: Any) -> Any | None:
    doc = _active_document(ctx)
    if doc is None or not is_writer(doc):
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


def notebook_session_id(ctx: Any, doc: Any | None = None) -> str | None:
    """Return ``notebook:…`` for a Writer document (always shared when interactive notebook is used)."""
    target = doc if doc is not None else _writer_document(ctx)
    if target is None or not is_writer(target):
        return None
    return f"notebook:{_workbook_session_key(target)}"


def reset_notebook_python_session(ctx: Any) -> None:
    """Menubar path: reset shared Python namespace for the active Writer notebook document."""
    doc = _writer_document(ctx)
    if doc is None:
        msgbox(
            ctx,
            "WriterAgent",
            _(
                "Reset Python Session for notebooks applies to LibreOffice Writer. "
                "Open a Writer document with an imported Jupyter notebook and try again."
            ),
        )
        return
    if not has_notebook_registry(doc):
        msgbox(
            ctx,
            "WriterAgent",
            _(
                "This Writer document has no imported notebook registry. "
                "Use Tools → Import Jupyter Notebook… first."
            ),
        )
        return

    session_id = notebook_session_id(ctx, doc)
    if not session_id:
        msgbox(ctx, "WriterAgent", _("Could not resolve notebook Python session."))
        return

    res = reset_python_session(ctx, session_id)
    if res.get("status") == "ok":
        msgbox(ctx, "WriterAgent", _("Notebook Python session reset for this document."))
        return

    msg = res.get("message") or _("Could not reset Python session.")
    msgbox(ctx, "WriterAgent", _("Error: {0}").format(msg))


def _reset_calc_shared_session(ctx: Any) -> None:
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


def reset_workbook_python_session(ctx: Any) -> None:
    """Menubar handler: reset notebook kernel (Writer) or shared Calc workbook session."""
    doc = _active_document(ctx)
    if doc is not None and is_writer(doc) and has_notebook_registry(doc):
        reset_notebook_python_session(ctx)
        return
    if doc is not None and is_writer(doc):
        msgbox(
            ctx,
            "WriterAgent",
            _(
                "This Writer document has no imported notebook registry. "
                "Use Tools → Import Jupyter Notebook… to enable notebook Python session reset."
            ),
        )
        return
    _reset_calc_shared_session(ctx)
