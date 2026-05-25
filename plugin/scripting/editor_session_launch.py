# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared Monaco editor session startup for Calc cell edit and Run Python Script."""

from __future__ import annotations

import logging
from typing import Any, Callable

from plugin.chatbot.dialogs import msgbox
from plugin.framework.i18n import _
from plugin.scripting.editor_bridge import EditorSession, _PERSISTENT_EDITOR, set_active_session
from plugin.scripting.editor_diagnostics import failure_message
from plugin.scripting.editor_launcher import probe_webview_import, resolve_editor_python, spawn_editor_process

log = logging.getLogger(__name__)


def monaco_editor_available(ctx: Any) -> tuple[str | None, bool]:
    """Return (venv python exe, True) when Monaco can launch, else (exe or None, False)."""
    exe, err = resolve_editor_python(ctx)
    if not exe:
        log.debug("monaco_editor_available: no venv python (%s)", err)
        return None, False
    webview_ok, detail = probe_webview_import(exe)
    if not webview_ok:
        log.debug("monaco_editor_available: webview probe failed for %s: %s", exe, detail[:200] if detail else "")
        return exe, False
    return exe, True


def launch_monaco_editor(
    ctx: Any,
    *,
    exe: str,
    load_message: dict[str, Any],
    on_save: Callable[..., dict[str, Any]],
    on_closed: Callable[[], None] | None = None,
) -> bool:
    """Start or reuse the Monaco child process and send *load_message*. Return True on success."""
    closed_handler = on_closed if on_closed is not None else (lambda: None)

    if _PERSISTENT_EDITOR.is_running:
        log.info("editor_session_launch: reusing running Monaco background process")
        proc = _PERSISTENT_EDITOR.proc
        assert proc is not None
        session = EditorSession(proc, on_save=on_save, on_closed=closed_handler)
        set_active_session(session)
    else:
        log.info("editor_session_launch: spawning new Monaco background process")
        try:
            proc = spawn_editor_process(exe)
        except OSError as e:
            log.exception("Failed to spawn editor")
            msgbox(ctx, "WriterAgent", failure_message(_("Could not start the Python editor."), exc=e))
            return False

        session = EditorSession(proc, on_save=on_save, on_closed=closed_handler)
        set_active_session(session)
        session.start_reader()

        if not session.wait_for_ready(ctx, timeout_sec=45.0):
            detail = session.read_stderr_tail()
            set_active_session(None)
            msgbox(ctx, "WriterAgent", failure_message(_("The Python editor window did not start."), detail=detail))
            return False

    if not session.is_running:
        detail = session.read_stderr_tail()
        set_active_session(None)
        msgbox(
            ctx,
            "WriterAgent",
            failure_message(_("The Python editor exited before it could load your code."), detail=detail),
        )
        return False

    try:
        session.send(load_message)
    except Exception as e:
        log.exception("Failed to send load to editor")
        set_active_session(None)
        msgbox(
            ctx,
            "WriterAgent",
            failure_message(_("Could not talk to the Python editor."), detail=session.read_stderr_tail(), exc=e),
        )
        return False

    return True
