# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibrePy Settings: Python tab only (no LLM General/Image pages)."""

from __future__ import annotations

import logging
from typing import Any

from plugin.chatbot.dialogs import (
    TabListener,
    get_checkbox_state,
    get_control_text,
    get_optional,
    is_checkbox_control,
    load_writeragent_dialog,
    set_checkbox_state,
    set_control_text,
    set_control_visible,
    translate_dialog,
)
from plugin.chatbot.listeners import BaseActionListener
from plugin.framework.config import as_bool
from plugin.framework.i18n import _
from plugin.framework.logging import init_logging
from plugin.scripting.venv_probe_ui import ScriptingVenvTestListener, VenvProbeProgressDialog

log = logging.getLogger(__name__)

_SCRIPTING_TAB_PAGE = 3
_LIBREPY_HIDDEN_SCRIPTING_CONTROLS = (
    "scripting__ppt_master_data_path",
    "label_scripting__ppt_master_data_path",
    "scripting__test_ppt_master_data",
)


class _DownloadVecPackListener(BaseActionListener):
    """Settings → Python: download Cython serialization binary (LibrePy; no audio deps)."""

    def __init__(self, ctx: Any, dlg: Any) -> None:
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent) -> None:
        from plugin.scripting.audio_recorder_service import run_vec_pack_download

        def probe(on_display, on_status):
            ok = run_vec_pack_download(on_display, on_status)
            return ok, ""

        VenvProbeProgressDialog(self._ctx, parent_dlg=self._dlg).run_modal_probe(
            probe, title=_("Cython Accelerator Download")
        )


def _scripting_field_specs() -> list[dict[str, Any]]:
    """Build SettingsDialog field specs for scripting.* keys (no LLM settings imports)."""
    from plugin.chatbot.settings_fields import build_module_field_specs

    return build_module_field_specs("scripting", control_ids="prefixed", skip_librepy_exclude=True)


def _set_ctrl_options(ctrl: Any, field: dict[str, Any]) -> None:
    opts = field.get("options")
    if not isinstance(opts, list):
        return
    labels = tuple(_(str(o.get("label", o.get("value", "")))) for o in opts if isinstance(o, dict))
    model = ctrl.getModel()
    if hasattr(model, "StringItemList"):
        model.StringItemList = labels


def _hide_settings_controls(dlg: Any, control_ids: tuple[str, ...]) -> None:
    """Hide optional XDL controls (e.g. WriterAgent-only rows on cached dialogs)."""
    for control_id in control_ids:
        ctrl = get_optional(dlg, control_id)
        if ctrl is not None:
            set_control_visible(ctrl, False)


def _configure_librepy_settings_chrome(dlg: Any) -> None:
    """Hide General/Image tabs and show Python-only settings chrome."""
    for tab_id in ("btn_tab_chat", "btn_tab_image"):
        tab = get_optional(dlg, tab_id)
        if tab is not None:
            set_control_visible(tab, False)

    scripting_tab = get_optional(dlg, "btn_tab_scripting")
    if scripting_tab is not None:
        try:
            scripting_tab.getModel().PositionX = 5
        except Exception:
            log.debug("Could not reposition Python settings tab", exc_info=True)
        scripting_tab.addActionListener(TabListener(dlg, _SCRIPTING_TAB_PAGE))

    _hide_settings_controls(dlg, _LIBREPY_HIDDEN_SCRIPTING_CONTROLS)


def _populate_field(ctrl: Any, field: dict[str, Any]) -> None:
    field_type = str(field.get("type") or "")
    if is_checkbox_control(ctrl):
        set_checkbox_state(ctrl, 1 if as_bool(field["value"]) else 0)
        return
    if field_type in ("int", "float") and hasattr(ctrl, "setValue"):
        try:
            ctrl.setValue(float(field["value"]))
            return
        except Exception:
            log.debug("setValue failed for %s", field.get("name"), exc_info=True)
    if hasattr(ctrl, "setText"):
        if "options" in field:
            _set_ctrl_options(ctrl, field)
        ctrl.setText(str(field.get("value", "")))
        return
    set_control_text(ctrl, field["value"])


def _extract_field(ctrl: Any) -> str:
    if is_checkbox_control(ctrl):
        return "true" if get_checkbox_state(ctrl) else "false"
    if hasattr(ctrl, "getValue"):
        try:
            return str(ctrl.getValue())
        except Exception:
            log.debug("getValue failed", exc_info=True)
    if hasattr(ctrl, "getText"):
        return ctrl.getText()
    return get_control_text(ctrl)


def open_librepy_settings(ctx: Any) -> None:
    """Open SettingsDialog on the Python (scripting) tab only."""
    from plugin.chatbot.settings_fields import apply_field_specs_result

    init_logging(ctx)
    log.warning("LibrePy settings: opening dialog")

    try:
        dlg = load_writeragent_dialog("SettingsDialog", ctx)
    except Exception:
        log.exception("LibrePy settings: dialog load failed")
        raise

    if dlg is None:
        log.error("LibrePy settings: SettingsDialog failed to load (null dialog)")
        return

    # UNO multi-page dialogs use model.Step (not Page); setPropertyValue("Page") fails on Linux.
    dlg.getModel().Step = _SCRIPTING_TAB_PAGE
    _configure_librepy_settings_chrome(dlg)

    field_specs = _scripting_field_specs()
    for field in field_specs:
        ctrl = get_optional(dlg, field["name"])
        if ctrl is None:
            log.warning("LibrePy settings: missing control %r", field["name"])
            continue
        try:
            _populate_field(ctrl, field)
        except Exception:
            log.exception("LibrePy settings: populate failed for %r", field["name"])
            raise

    translate_dialog(dlg)
    try:
        dlg.getModel().Title = _("Python Settings")
    except Exception:
        pass

    test_btn = get_optional(dlg, "scripting__test_venv")
    test_listener = None
    if test_btn is not None:
        test_listener = ScriptingVenvTestListener(ctx, dlg)
        test_btn.addActionListener(test_listener)

    download_btn = get_optional(dlg, "scripting__download_audio_binaries")
    download_listener = None
    if download_btn is not None:
        download_listener = _DownloadVecPackListener(ctx, dlg)
        download_btn.addActionListener(download_listener)

    try:
        if dlg.execute():
            result: dict[str, Any] = {}
            for field in field_specs:
                ctrl = get_optional(dlg, field["name"])
                if ctrl is None:
                    continue
                result[field["name"]] = _extract_field(ctrl)
            apply_field_specs_result(ctx, result, field_specs)
    finally:
        if test_btn is not None and test_listener is not None:
            try:
                test_btn.removeActionListener(test_listener)
            except Exception:
                pass
        if download_btn is not None and download_listener is not None:
            try:
                download_btn.removeActionListener(download_listener)
            except Exception:
                pass
        dlg.dispose()
