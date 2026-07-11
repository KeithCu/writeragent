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
    set_control_enabled,
    set_control_text,
    set_control_visible,
    translate_dialog,
)
from plugin.chatbot.listeners import BaseActionListener
from plugin.framework.config import as_bool, get_config, set_config
from plugin.framework.event_bus import global_event_bus
from plugin.framework.i18n import _
from plugin.framework.logging import init_logging
from plugin.framework.queue_executor import post_to_main_thread
from plugin.framework.uno_context import get_extension_url, get_toolkit
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)

_SCRIPTING_TAB_PAGE = 3
_MODULE_NAME = "scripting"
_LIBREPY_HIDDEN_SCRIPTING_CONTROLS = (
    "scripting__ppt_master_data_path",
    "label_scripting__ppt_master_data_path",
    "scripting__test_ppt_master_data",
)


class _VenvProbeProgressDialog:
    """Modal progress window for Settings → Python Test (probe runs in a worker thread)."""

    def __init__(self, ctx: Any, parent_dlg: Any = None) -> None:
        self._ctx = ctx
        self._parent_dlg = parent_dlg
        self._dlg = None

    def run_modal_probe(self, probe_fn) -> None:
        self._create_dialog()

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
                log.exception("LibrePy venv probe failed")

                def error_ui(exc=exc) -> None:
                    self.set_display(str(exc))
                    self.finish(_("Venv check failed"), False)

                post_to_main_thread(error_ui)

        run_in_background(work, name="librepy-settings-venv-test")
        dlg = self._dlg
        assert dlg is not None
        try:
            dlg.execute()
        finally:
            self._dispose()

    def _create_dialog(self) -> None:
        dlg = load_writeragent_dialog("PythonTestProgressDialog", self._ctx)
        if dlg is None:
            raise RuntimeError("Failed to load PythonTestProgressDialog")
        self._dlg = dlg
        btn_close = dlg.getControl("BtnClose")
        if btn_close is not None:
            btn_close.addActionListener(_VenvProbeCloseListener(self))

    def set_display(self, text: str) -> None:
        if self._dlg is None:
            return
        set_control_text(self._dlg.getControl("LogArea"), text)
        self._pump_events()

    def set_status(self, text: str) -> None:
        if self._dlg is None:
            return
        status = text.strip() or _("Testing Python environment...")
        if len(status) > 80:
            status = status[:77] + "..."
        set_control_text(self._dlg.getControl("StatusLbl"), status)
        self._pump_events()

    def finish(self, title: str, ok: bool) -> None:
        if self._dlg is None:
            return
        try:
            self._dlg.getModel().Title = _(title)
        except Exception:
            pass
        set_control_text(self._dlg.getControl("StatusLbl"), _("Done") if ok else _("Failed"))
        set_control_enabled(self._dlg.getControl("BtnClose"), True)
        self._pump_events()

    def _dispose(self) -> None:
        dlg = self._dlg
        self._dlg = None
        if dlg is None:
            return
        try:
            dlg.dispose()
        except Exception:
            log.debug("Failed to dispose venv probe progress dialog", exc_info=True)

    def _pump_events(self) -> None:
        toolkit = get_toolkit(self._ctx)
        if toolkit and hasattr(toolkit, "processEventsToIdle"):
            toolkit.processEventsToIdle()


class _VenvProbeCloseListener(BaseActionListener):
    def __init__(self, progress: _VenvProbeProgressDialog) -> None:
        self._progress = progress

    def on_action_performed(self, rEvent) -> None:
        dlg = self._progress._dlg
        if dlg is not None:
            try:
                dlg.endDialog(0)
            except Exception:
                log.debug("Failed to close venv probe progress dialog", exc_info=True)


class _ScriptingVenvTestListener(BaseActionListener):
    def __init__(self, ctx: Any, dlg: Any) -> None:
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent) -> None:
        from plugin.scripting.audio_recorder_service import ensure_downloaded_audio_on_path
        from plugin.scripting.payload_codec import host_cython_status_line
        from plugin.scripting.venv_worker import probe_venv_path_with_progress

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

        _VenvProbeProgressDialog(self._ctx, parent_dlg=self._dlg).run_modal_probe(probe)


class _DownloadVecPackListener(BaseActionListener):
    """Settings → Python: download Cython serialization binary (LibrePy; no audio deps)."""

    def __init__(self, ctx: Any, dlg: Any) -> None:
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent) -> None:
        from plugin.scripting.audio_recorder_service import run_vec_pack_download

        progress = _VenvProbeProgressDialog(self._ctx, parent_dlg=self._dlg)
        if progress._dlg is not None:
            try:
                progress._dlg.getModel().Title = _("Cython Accelerator Download")
            except Exception:
                pass

        def probe(on_display, on_status):
            ok = run_vec_pack_download(on_display, on_status)
            return ok, ""

        progress.run_modal_probe(probe)


def _scripting_field_specs() -> list[dict[str, Any]]:
    """Build SettingsDialog field specs for scripting.* keys (no LLM settings imports)."""
    try:
        from plugin._manifest import MODULES
    except ImportError:
        return []

    manifest: dict[str, Any] | None = None
    for m in MODULES:
        if not isinstance(m, dict):
            continue
        if str(m.get("name", "")) == _MODULE_NAME:
            manifest = m
            break
    if manifest is None:
        return []

    field_specs: list[dict[str, Any]] = []
    m_config = manifest.get("config") or {}
    if not isinstance(m_config, dict):
        return field_specs

    for field_name, schema in m_config.items():
        if not isinstance(schema, dict):
            continue
        if schema.get("internal") or schema.get("widget") == "list_detail":
            continue
        if schema.get("settings_persist") is False:
            continue
        if schema.get("librepy_exclude"):
            continue

        ctrl_id = f"{_MODULE_NAME}__{field_name}"
        config_key = f"{_MODULE_NAME}.{field_name}"
        val = get_config(config_key)
        opts = schema.get("options", [])
        if isinstance(opts, list) and opts and isinstance(opts[0], dict):
            v_str = str(val).strip().lower()
            for opt in opts:
                if isinstance(opt, dict) and str(opt.get("value", "")) == v_str:
                    val = _(str(opt.get("label", val)))
                    break

        field: dict[str, Any] = {"name": ctrl_id, "config_key": config_key, "value": str(val)}
        if schema.get("options"):
            field["options"] = schema["options"]
        schema_type = schema.get("type", "string")
        if schema_type == "boolean":
            schema_type = "bool"
        if schema_type in ("bool", "int", "float"):
            field["type"] = str(schema_type)
            if schema_type == "bool":
                field["value"] = "true" if as_bool(val) else "false"
        field_specs.append(field)
    return field_specs


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


def _load_settings_dialog(ctx: Any) -> Any:
    """Load SettingsDialog with DialogProvider (matches WriterAgent; DP2 breaks on Linux)."""
    smgr = ctx.getServiceManager()
    base_url = get_extension_url(ctx)
    dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", ctx)
    return dp.createDialog(base_url + "/WriterAgentDialogs/SettingsDialog.xdl")


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


def _apply_scripting_result(result: dict[str, Any], field_specs: list[dict[str, Any]]) -> None:
    by_ctrl = {f["name"]: f for f in field_specs}
    for ctrl_id, val in result.items():
        spec = by_ctrl.get(ctrl_id)
        if spec is None:
            continue
        save_key = str(spec.get("config_key") or ctrl_id.replace("__", "."))
        set_config(save_key, val)


def open_librepy_settings(ctx: Any) -> None:
    """Open SettingsDialog on the Python (scripting) tab only."""
    init_logging(ctx)
    log.warning("LibrePy settings: opening dialog")

    try:
        dlg = _load_settings_dialog(ctx)
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
        test_listener = _ScriptingVenvTestListener(ctx, dlg)
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
            _apply_scripting_result(result, field_specs)
            global_event_bus.emit("config:changed", ctx=ctx)
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
