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
    load_writeragent_dialog_detail,
    msgbox,
    set_control_enabled,
    set_control_text,
)
from plugin.framework.uno_listeners import BaseActionListener
from plugin.framework.i18n import N_, _
from plugin.framework.queue_executor import post_to_main_thread
from plugin.framework.thread_guard import on_main_thread
from plugin.framework.uno_context import get_desktop, get_toolkit, process_events_to_idle
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)

# probe_fn(on_display, on_status) -> (ok, message)
ProbeFn = Callable[[Callable[[str], None], Callable[[str], None]], tuple[bool, str]]


def _dialog_parent_for_child(ctx: Any, parent_dlg: Any) -> Any:
    """Resolve a parent window for a child modal opened above an executing dialog."""
    if parent_dlg is not None:
        try:
            peer = parent_dlg.getPeer()
            if peer is not None:
                return peer
        except Exception:
            log.debug("parent_dlg.getPeer failed for child modal", exc_info=True)
    try:
        desktop = get_desktop(ctx)
        frame = desktop.getCurrentFrame() if desktop else None
        if frame is not None:
            return frame.getContainerWindow()
    except Exception:
        log.debug("getCurrentFrame parent fallback failed for child modal", exc_info=True)
    return None


class VenvProbeProgressDialog:
    """Modal progress window for Settings → Python Test (probe runs in a worker thread)."""

    _ctx: Any
    _parent_dlg: Any
    _dlg: Any
    _append_log: bool
    _log_parts: list[str]
    _pumping: bool

    def __init__(self, ctx: Any, parent_dlg: Any = None) -> None:
        self._ctx = ctx
        self._parent_dlg = parent_dlg
        self._dlg = None
        self._append_log = False
        self._log_parts = []
        self._pumping = False

    def run_modal_probe(
        self,
        probe_fn: ProbeFn,
        *,
        title: str | None = None,
        success_title: str | None = None,
        failure_title: str | None = None,
        append_log: bool = False,
    ) -> None:
        """Show a modal dialog and run *probe_fn(on_display, on_status)* in a worker."""
        try:
            if not self._create_dialog(title=title):
                return
            self._append_log = append_log
            self._log_parts = []
            dlg = self._dlg
            if dlg is None:
                return

            def on_display(text: str) -> None:
                post_to_main_thread(lambda body=text: self.set_display(body))

            def on_status(text: str) -> None:
                post_to_main_thread(lambda status=text: self.set_status(status))

            def work() -> None:
                try:
                    ok, msg = probe_fn(on_display, on_status)

                    def finish_ui() -> None:
                        if not ok and msg and self._dlg is not None:
                            log_ctrl = self._dlg.getControl("LogArea")
                            if not get_control_text(log_ctrl).strip():
                                self.set_display(msg)
                        self.finish(success_title if ok else failure_title, ok)

                    post_to_main_thread(finish_ui)
                except BaseException as exc:
                    log.exception("Scripting venv probe failed")

                    def error_ui(exc: BaseException = exc) -> None:
                        self.set_display(str(exc))
                        self.finish(failure_title, False)

                    post_to_main_thread(error_ui)
                    if not isinstance(exc, Exception):
                        raise

            run_in_background(work, name="settings-venv-test")
            dlg.execute()
        finally:
            self._dispose()

    def _create_dialog(self, *, title: str | None = None) -> bool:
        dlg, detail = load_writeragent_dialog_detail("PythonTestProgressDialog", self._ctx)
        if dlg is None:
            log.warning("Failed to load PythonTestProgressDialog: %s", detail)
            msgbox(
                self._ctx,
                N_("Python Test"),
                _("Could not open the Python test progress dialog."),
                box_type=3,
            )
            return False
        self._dlg = dlg
        if title:
            dlg.getModel().Title = _(title)
        btn_close = dlg.getControl("BtnClose")
        if btn_close is not None:
            # Bugfix: Close was clickable during the probe, so execute() returned and
            # _dispose() ran while the worker was still writing the download.
            # The programmatic dialog passed enabled=False (5b3496e1). The XDL
            # conversion (28bb44c1) dropped that and left the stock enabled button.
            # finish() is what turns the button back on. The title bar stays closeable.
            set_control_enabled(btn_close, False)
            btn_close.addActionListener(_VenvProbeCloseListener(self))
        self._parent_progress_dialog(dlg)
        return True

    def _parent_progress_dialog(self, dlg: Any) -> None:
        """Parent this modal to Settings before execute().

        Bugfix: the progress window was an unparented top-level dialog, so it
        was not anchored to Settings. _parent_dlg was stored and never read.
        The XDL conversion (28bb44c1) stopped calling createPeer. DialogProvider
        does not create a peer; execute() then builds one with no parent.
        setVisible(False) then createPeer matches LibreOffice
        wizards/ui/UnoDialog.py createWindowPeer; executeDialog runs execute()
        on that peer. A second createPeer throws, so an existing peer is left
        alone. Failure here still leaves execute() able to open the dialog.
        """
        parent = _dialog_parent_for_child(self._ctx, self._parent_dlg)
        try:
            if dlg.getPeer() is not None:
                return
        except Exception:
            log.debug("venv probe progress getPeer failed", exc_info=True)
        try:
            toolkit = get_toolkit(self._ctx)
            if toolkit is None:
                return
            dlg.setVisible(False)
            dlg.createPeer(toolkit, parent)
        except Exception:
            log.debug("venv probe progress createPeer failed", exc_info=True)

    def _pump_dialog(self) -> None:
        # finish() is posted from the probe worker via post_to_main_thread.
        # Opengrep intrafile taint still treats process_events_to_idle in
        # finish() as a worker sink (uno-off-main-thread). A bare
        # if on_main_thread() is the documented sanitizer; pumping VCL
        # off-main is never correct.
        if not on_main_thread():
            return
        # Bugfix: processEventsToIdle from inside process_queue dispatches the
        # next AsyncCallback. Download on_status fires every 64 KB, so the nested
        # set_display -> pump chain grew with the queue. The flag lets the outer
        # pump drain those callbacks without entering processEventsToIdle again.
        if self._pumping:
            return
        self._pumping = True
        try:
            process_events_to_idle(self._ctx)
        finally:
            self._pumping = False

    def set_display(self, text: str) -> None:
        if self._dlg is None:
            return
        # Bugfix: LogArea showed only the last download line.
        # run_vec_pack_download and run_audio_download call on_display once per
        # line. set_control_text replaces. The venv and PPT probes already pass
        # a full body, so replace stays the default.
        # append_log concatenates fragments on the dialog. The parts list is the
        # source of truth so a failed getText cannot wipe earlier lines.
        shown = text
        if self._append_log:
            self._log_parts.append(text)
            shown = "".join(self._log_parts)
        set_control_text(self._dlg.getControl("LogArea"), shown)
        self._pump_dialog()

    def set_status(self, text: str) -> None:
        if self._dlg is None:
            return
        status = text.strip() or _("Testing Python environment...")
        if len(status) > 80:
            status = status[:77] + "..."
        set_control_text(self._dlg.getControl("StatusLbl"), status)
        self._pump_dialog()

    def finish(self, title: str | None, ok: bool) -> None:
        if self._dlg is None:
            return
        # Bugfix: every caller ended as "Venv OK" / "Venv check failed", including
        # the audio download, the Cython download, and the PPT-Master test.
        # run_modal_probe always passed those two strings into finish, and finish
        # wrote them over the title the caller had set.
        # None keeps the opening title. A msgid is translated once here.
        if title:
            try:
                self._dlg.getModel().Title = _(title)
            except Exception:
                log.debug("venv probe dialog title failed", exc_info=True)
        set_control_text(self._dlg.getControl("StatusLbl"), _("Done") if ok else _("Failed"))
        set_control_enabled(self._dlg.getControl("BtnClose"), True)
        self._pump_dialog()

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
    _progress: VenvProbeProgressDialog

    def __init__(self, progress: VenvProbeProgressDialog) -> None:
        self._progress = progress

    def on_action_performed(self, rEvent: Any) -> None:
        dlg = self._progress._dlg
        if dlg is not None:
            try:
                dlg.endDialog(0)
            except Exception:
                log.debug("Failed to close venv probe progress dialog", exc_info=True)


class ScriptingVenvTestListener(BaseActionListener):
    """Settings → Python: run a quick subprocess check using the path in the text field (saved or not)."""

    _ctx: Any
    _dlg: Any
    _include_vector_search: bool
    _include_audio: bool

    def __init__(
        self,
        ctx: Any,
        dlg: Any,
        *,
        include_vector_search: bool = True,
        include_audio: bool = True,
    ) -> None:
        self._ctx = ctx
        self._dlg = dlg
        self._include_vector_search = include_vector_search
        self._include_audio = include_audio

    def on_action_performed(self, rEvent: Any) -> None:
        from plugin.scripting.native_binaries import ensure_native_binaries_on_path
        from plugin.scripting.payload_codec import host_cython_status_line
        from plugin.scripting.venv_diagnostics import probe_venv_path_with_progress

        # User-downloaded writeragent_vec may be on sys.path via audio_binaries.
        ensure_native_binaries_on_path()
        # Reload on the main thread (not the probe worker): after redownload the
        # worker must not be the first site that imports / calls into natives.
        cython_status = host_cython_status_line(reload=True)

        path_ctrl = get_optional(self._dlg, "scripting__python_venv_path")
        raw = get_control_text(path_ctrl) if path_ctrl else ""

        def probe(on_display: Callable[[str], None], on_status: Callable[[str], None]) -> tuple[bool, str]:
            return probe_venv_path_with_progress(
                raw,
                on_display,
                on_status=on_status,
                extra_lines_after_header=(cython_status,),
                include_vector_search=self._include_vector_search,
                include_audio=self._include_audio,
            )

        VenvProbeProgressDialog(self._ctx, parent_dlg=self._dlg).run_modal_probe(
            probe,
            success_title=N_("Venv OK"),
            failure_title=N_("Venv check failed"),
        )
