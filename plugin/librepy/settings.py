# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibrePy Settings: Python tab only (no LLM General/Image pages)."""

from __future__ import annotations

import logging
from typing import Any, Callable

from plugin.chatbot.dialogs import TabListener, get_optional, load_writeragent_dialog_detail, msgbox, set_control_text, set_control_visible
from plugin.chatbot.settings_fields import apply_field_specs_result, build_module_field_specs, populate_settings_control, read_settings_control
from plugin.framework.errors import ConfigValidationError, suppress_disposed
from plugin.framework.uno_listeners import BaseActionListener
from plugin.framework.i18n import N_, _
from plugin.framework.logging import init_logging
from plugin.scripting.venv_probe_ui import ScriptingVenvTestListener, VenvProbeProgressDialog

log = logging.getLogger(__name__)

_SCRIPTING_TAB_PAGE = 3  # SettingsDialog.xdl Python page (page 3 of the multi-page dialog)


class _DownloadVecPackListener(BaseActionListener):
    """Settings → Python: download Cython serialization binary (LibrePy; no audio deps)."""

    _ctx: Any
    _dlg: Any

    def __init__(self, ctx: Any, dlg: Any) -> None:
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent: Any) -> None:
        from plugin.framework.queue_executor import execute_on_main_thread
        from plugin.scripting.native_binaries import ensure_native_binaries_on_path, run_vec_pack_download
        from plugin.scripting.payload_codec import invalidate_host_cython_accelerator

        def bind_downloaded_vec_on_main() -> None:
            # sys.path insert plus sys.modules and accelerator globals.
            # ScriptingVenvTestListener does the same work on this thread
            # before its probe; the download cannot, because the files are
            # not on disk yet.
            ensure_native_binaries_on_path()
            invalidate_host_cython_accelerator()

        def probe(on_display: Callable[[str], None], on_status: Callable[[str], None]) -> tuple[bool, str]:
            # The download stays on the probe worker. Appending audio_binaries
            # to sys.path and clearing the Cython accelerator must run on the
            # main thread via execute_on_main_thread. VenvProbeProgressDialog
            # runs probe_fn with run_in_background while execute() pumps VCL;
            # that nested loop already dispatches AsyncCallback for the
            # progress lines, so the bind does not wait on a thread blocked
            # outside the message loop.
            ok = run_vec_pack_download(on_display, on_status)
            if ok:
                execute_on_main_thread(bind_downloaded_vec_on_main)
            return ok, ""

        VenvProbeProgressDialog(self._ctx, parent_dlg=self._dlg).run_modal_probe(
            probe,
            title=N_("Cython Accelerator Download"),
            append_log=True,
        )


def _configure_librepy_settings_chrome(dlg: Any) -> None:
    """Hide General/Image tabs and show Python-only settings chrome."""
    for tab_id in ("btn_tab_chat", "btn_tab_image"):
        tab = get_optional(dlg, tab_id)
        if tab is not None:
            set_control_visible(tab, False)

    scripting_tab = get_optional(dlg, "btn_tab_scripting")
    if scripting_tab is not None:
        scripting_tab.addActionListener(TabListener(dlg, _SCRIPTING_TAB_PAGE))

    download_label = get_optional(dlg, "label_scripting__download_audio_binaries")
    if download_label is not None:
        set_control_text(download_label, _("Download Cython serialization binary:"))


def open_librepy_settings(ctx: Any) -> None:
    """Open SettingsDialog on the Python (scripting) tab only."""
    init_logging(ctx)
    log.debug("LibrePy settings: opening dialog")

    dlg, load_detail = load_writeragent_dialog_detail("SettingsDialog", ctx)
    if dlg is None:
        log.error("LibrePy settings: SettingsDialog failed to load (null dialog)")
        detail = f"\n\n{load_detail}" if load_detail else ""
        msgbox(ctx, _("Python Settings"), _("Could not open Settings.") + detail, box_type=3)
        return

    # SettingsDialog.show wraps create, populate, and execute in one
    # try/finally that disposes. Running Step, chrome, populate, and the
    # title before that try leaked the UNO dialog on a raise.
    # UNO multi-page dialogs use model.Step (not Page); setPropertyValue("Page") fails on Linux.
    attached: list[tuple[Any, Any]] = []
    try:
        dlg.getModel().Step = _SCRIPTING_TAB_PAGE
        _configure_librepy_settings_chrome(dlg)
        dlg.getModel().Title = _("Python Settings")

        button_listeners = [
            (
                "scripting__test_venv",
                lambda: ScriptingVenvTestListener(ctx, dlg, include_vector_search=False, include_audio=False),
            ),
            ("scripting__download_audio_binaries", lambda: _DownloadVecPackListener(ctx, dlg)),
        ]
        for ctrl_id, factory in button_listeners:
            btn = get_optional(dlg, ctrl_id)
            if btn is not None:
                listener = factory()
                attached.append((btn, listener))
                btn.addActionListener(listener)

        field_specs = build_module_field_specs("scripting", control_ids="prefixed", skip_librepy_exclude=True)
        populated: list[tuple[Any, dict[str, Any]]] = []
        for field in field_specs:
            ctrl = get_optional(dlg, field["name"])
            if ctrl is None:
                log.warning("LibrePy settings: missing control %r", field["name"])
                continue
            try:
                populate_settings_control(ctrl, field)
            except Exception:
                log.exception("LibrePy settings: populate failed for %r", field["name"])
                raise
            populated.append((ctrl, field))

        while dlg.execute():
            result: dict[str, Any] = {}
            for ctrl, field in populated:
                result[field["name"]] = read_settings_control(ctrl, field)
            try:
                apply_field_specs_result(ctx, result, field_specs)
                break
            except ConfigValidationError as ve:
                msgbox(ctx, _("Invalid Setting"), str(ve))
    finally:
        # Do not return from finally: that would swallow a populate error.
        with suppress_disposed("detach LibrePy settings listeners", logger=log):
            for btn, listener in attached:
                btn.removeActionListener(listener)
        with suppress_disposed("close LibrePy settings", logger=log):
            dlg.dispose()
