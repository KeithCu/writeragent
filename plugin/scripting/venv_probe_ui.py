# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared Settings → Python modal progress UI (venv probe / download workers)."""

from __future__ import annotations

import logging
from typing import Any, Callable

from plugin.chatbot.dialogs import (
    get_control_text,
    get_optional,
    load_writeragent_dialog,
    set_control_enabled,
    set_control_text,
)
from plugin.chatbot.listeners import BaseActionListener
from plugin.framework.i18n import _
from plugin.framework.queue_executor import post_to_main_thread
from plugin.framework.uno_context import process_events_to_idle
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)

# probe_fn(on_display, on_status) -> (ok, message)
ProbeFn = Callable[[Callable[[str], None], Callable[[str], None]], tuple[bool, str]]


class VenvProbeProgressDialog:
    """Modal progress window for Settings → Python Test (probe runs in a worker thread)."""

    def __init__(self, ctx: Any, parent_dlg: Any = None) -> None:
        self._ctx = ctx
        self._parent_dlg = parent_dlg
        self._dlg = None

    def run_modal_probe(self, probe_fn: ProbeFn, *, title: str | None = None) -> None:
        """Show a modal dialog and run *probe_fn(on_display, on_status)* in a worker."""
        self._create_dialog(title=title)

        def on_display(text: str) -> None:
            post_to_main_thread(lambda body=text: self.set_display(body))

        def on_status(text: str) -> None:
            post_to_main_thread(lambda status=text: self.set_status(status))

        def work() -> None:
            try:
                ok, _msg = probe_fn(on_display, on_status)

                def finish_ui() -> None:
                    self.finish(_("Venv OK") if ok else _("Venv check failed"), ok)

                post_to_main_thread(finish_ui)
            except Exception as exc:
                log.exception("Scripting venv probe failed")

                def error_ui(exc=exc) -> None:
                    self.set_display(str(exc))
                    self.finish(_("Venv check failed"), False)

                post_to_main_thread(error_ui)

        run_in_background(work, name="settings-venv-test")
        dlg = self._dlg
        assert dlg is not None
        try:
            dlg.execute()
        finally:
            self._dispose()

    def _create_dialog(self, *, title: str | None = None) -> None:
        dlg = load_writeragent_dialog("PythonTestProgressDialog", self._ctx)
        if dlg is None:
            raise RuntimeError("Failed to load PythonTestProgressDialog")
        self._dlg = dlg
        if title:
            try:
                dlg.getModel().Title = _(title)
            except Exception:
                log.debug("Failed to set venv probe progress title", exc_info=True)
        btn_close = dlg.getControl("BtnClose")
        if btn_close is not None:
            btn_close.addActionListener(_VenvProbeCloseListener(self))

    def set_display(self, text: str) -> None:
        if self._dlg is None:
            return
        set_control_text(self._dlg.getControl("LogArea"), text)
        process_events_to_idle(self._ctx)

    def set_status(self, text: str) -> None:
        if self._dlg is None:
            return
        status = text.strip() or _("Testing Python environment...")
        if len(status) > 80:
            status = status[:77] + "..."
        set_control_text(self._dlg.getControl("StatusLbl"), status)
        process_events_to_idle(self._ctx)

    def finish(self, title: str, ok: bool) -> None:
        if self._dlg is None:
            return
        try:
            self._dlg.getModel().Title = _(title)
        except Exception:
            pass
        set_control_text(self._dlg.getControl("StatusLbl"), _("Done") if ok else _("Failed"))
        set_control_enabled(self._dlg.getControl("BtnClose"), True)
        process_events_to_idle(self._ctx)

    def _dispose(self) -> None:
        dlg = self._dlg
        self._dlg = None
        if dlg is None:
            return
        try:
            dlg.dispose()
        except Exception:
            log.debug("Failed to dispose venv probe progress dialog", exc_info=True)


class _VenvProbeCloseListener(BaseActionListener):
    def __init__(self, progress: VenvProbeProgressDialog) -> None:
        self._progress = progress

    def on_action_performed(self, rEvent) -> None:
        dlg = self._progress._dlg
        if dlg is not None:
            try:
                dlg.endDialog(0)
            except Exception:
                log.debug("Failed to close venv probe progress dialog", exc_info=True)


class ScriptingVenvTestListener(BaseActionListener):
    """Settings → Python: run a quick subprocess check using the path in the text field (saved or not)."""

    def __init__(self, ctx: Any, dlg: Any) -> None:
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent) -> None:
        from plugin.scripting.audio_recorder_service import ensure_downloaded_audio_on_path
        from plugin.scripting.payload_codec import host_cython_status_line
        from plugin.scripting.venv_diagnostics import probe_venv_path_with_progress

        # User-downloaded writeragent_vec may be on sys.path via audio_binaries.
        ensure_downloaded_audio_on_path()

        path_ctrl = get_optional(self._dlg, "scripting__python_venv_path")
        raw = get_control_text(path_ctrl) if path_ctrl else ""

        def probe(on_display, on_status):
            return probe_venv_path_with_progress(
                raw,
                on_display,
                on_status=on_status,
                extra_lines_after_header=(host_cython_status_line(),),
            )

        VenvProbeProgressDialog(self._ctx, parent_dlg=self._dlg).run_modal_probe(probe)
