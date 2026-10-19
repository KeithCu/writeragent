# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Modeless module config dialogs generated from module.yaml config_dialog specs."""

from __future__ import annotations

import logging
from typing import Any, Callable

from plugin.chatbot.dialogs import TabListener, get_optional, load_writeragent_dialog_detail
from plugin.framework.errors import best_effort, suppress_disposed
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_listeners import BaseActionListener, BaseTopWindowListener

log = logging.getLogger(__name__)

_active_dialogs: dict[str, Any] = {}


class _CallbackActionListener(BaseActionListener):
    def __init__(self, callback: Callable[[Any], None]) -> None:
        self._callback: Callable[[Any], None] = callback

    def on_action_performed(self, rEvent: Any) -> None:
        self._callback(rEvent)

def _tab_buttons_in_order(dlg: Any) -> list[Any]:
    """btn_tab_* controls in dialog-model order (the order the XDL generator writes)."""
    from plugin.chatbot.dialogs import _dialog_model_element_names

    buttons: list[Any] = []
    for name in _dialog_model_element_names(dlg):
        if not str(name).startswith("btn_tab_"):
            continue
        btn = get_optional(dlg, str(name))
        if btn is not None:
            buttons.append(btn)
    return buttons


def get_module_config_dialog_id(module_name: str) -> str | None:
    from plugin.chatbot.settings_fields import find_module_manifest

    manifest = find_module_manifest(module_name)
    if not manifest:
        return None
    cfg_dialog = manifest.get("config_dialog") or {}
    dialog_id = str(cfg_dialog.get("id") or "").strip()
    return dialog_id or None


def get_module_config_field_specs(ctx: Any, module_name: str) -> list[dict[str, Any]]:
    """Field specs for a standalone module config dialog (flat control ids)."""
    from plugin.chatbot.settings_fields import build_module_field_specs

    return build_module_field_specs(module_name, ctx=ctx, control_ids="flat")


def apply_module_config_result(ctx: Any, module_name: str, result: dict[str, Any]) -> None:
    """Persist standalone module dialog values to writeragent.json."""
    from plugin.chatbot.settings_fields import apply_field_specs_result

    field_specs = get_module_config_field_specs(ctx, module_name)
    apply_field_specs_result(ctx, result, field_specs)


class ModuleConfigDialog:
    """Modeless settings dialog for one MODULES entry with config_dialog metadata."""

    _ctx: Any
    _module_name: str
    _closed: bool

    def __init__(self, ctx: Any, module_name: str) -> None:
        self._ctx = ctx
        self._module_name = module_name
        self._dlg: Any | None = None
        self._closed = False
        self._top_listener: Any | None = None

    @classmethod
    @main_thread_only
    def show(cls, ctx: Any, module_name: str) -> None:
        existing = _active_dialogs.get(module_name)
        if existing is not None:
            existing.close()
        dialog = cls(ctx, module_name)
        dialog._open()
        _active_dialogs[module_name] = dialog

    def _forget_active(self) -> None:
        # Pop only if this instance is still registered; windowClosing returns before
        # toolkit disposing, so a show() in between must stay registered.
        if _active_dialogs.get(self._module_name) is self:
            _active_dialogs.pop(self._module_name, None)

    def close(self, *, toolkit_teardown: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        self._forget_active()
        dlg = self._dlg
        self._dlg = None
        if dlg is None:
            return
        if toolkit_teardown:
            with suppress_disposed("module config: hide after windowClosing", logger=log):
                dlg.setVisible(False)
            return
        with suppress_disposed("module config: hide before dispose", logger=log):
            dlg.setVisible(False)
        with suppress_disposed("module config: dispose", logger=log):
            dlg.dispose()

    def _open(self) -> None:
        ctx = self._ctx
        dialog_id = get_module_config_dialog_id(self._module_name)
        if not dialog_id:
            raise RuntimeError("No config_dialog.id for module %s" % self._module_name)

        dlg, load_detail = load_writeragent_dialog_detail(dialog_id, ctx)
        # Raise, not log-and-return: a silent return left a dialog with _dlg None in _active_dialogs.
        if dlg is None:
            raise RuntimeError("load_detail: failed to load module dialog %s: %s" % (dialog_id, load_detail))

        self._dlg = dlg
        try:
            self._setup_tabs()
            self._wire_buttons()
            self._populate_fields(get_module_config_field_specs(ctx, self._module_name))

            owner = self

            class _TopWindowListener(BaseTopWindowListener):
                def on_window_closing(self, rEvent: Any) -> None:
                    owner.close(toolkit_teardown=True)

                def on_disposing(self, Source: Any) -> None:
                    owner._closed = True
                    owner._forget_active()
                    owner._dlg = None

            self._top_listener = _TopWindowListener()
            dlg.addTopWindowListener(self._top_listener)
            dlg.setVisible(True)
        except Exception:
            self._dlg = None
            best_effort(dlg.dispose, "Failed to dispose module config dialog")
            raise

    def _setup_tabs(self) -> None:
        assert self._dlg is not None
        # Step numbers follow control order, which is the page order the
        # generator writes. A new page: in module.yaml then gets a listener
        # without a second hardcoded map.
        for page_num, btn in enumerate(_tab_buttons_in_order(self._dlg), start=1):
            btn.addActionListener(TabListener(self._dlg, page_num))

    def _wire_buttons(self) -> None:
        assert self._dlg is not None
        apply_btn = get_optional(self._dlg, "btn_apply")
        if apply_btn:
            apply_btn.addActionListener(_CallbackActionListener(lambda ev: self._apply(close=False)))
        ok_btn = get_optional(self._dlg, "btn_ok")
        if ok_btn:
            ok_btn.addActionListener(_CallbackActionListener(lambda ev: self._apply(close=True)))
        close_btn = get_optional(self._dlg, "btn_close")
        if close_btn:
            close_btn.addActionListener(_CallbackActionListener(lambda ev: self.close()))

    def _populate_fields(self, field_specs: list[dict[str, Any]]) -> None:
        from plugin.chatbot.settings_fields import populate_settings_control

        assert self._dlg is not None
        for field in field_specs:
            ctrl = get_optional(self._dlg, field["name"])
            if ctrl is None:
                log.warning(
                    "Module config dialog %s missing control %r",
                    self._module_name,
                    field["name"],
                )
                continue
            populate_settings_control(ctrl, field)

    def _extract_result(self) -> dict[str, Any]:
        from plugin.chatbot.settings_fields import read_settings_control

        assert self._dlg is not None
        result: dict[str, Any] = {}
        for field in get_module_config_field_specs(self._ctx, self._module_name):
            name = field["name"]
            ctrl = get_optional(self._dlg, name)
            if ctrl is None:
                continue
            value = read_settings_control(ctrl, field)
            if value is None:
                continue
            # Helper returns bool. This dialog used to persist "true"/"false"
            # strings (LibrePy does the same wrap). WriterAgent Settings keeps
            # the bool.
            if isinstance(value, bool):
                result[name] = "true" if value else "false"
            else:
                result[name] = value
        return result

    def _apply(self, *, close: bool) -> None:
        from plugin.chatbot.dialogs import msgbox
        from plugin.framework.i18n import _

        try:
            result = self._extract_result()
            apply_module_config_result(self._ctx, self._module_name, result)
        except Exception as exc:
            # A failed save stays open. Closing here dismisses the dialog
            # after the values were not stored.
            log.exception("Failed to apply module config for %s", self._module_name)
            msgbox(self._ctx, _("Invalid Setting"), str(exc))
            return
        if close:
            self.close()


def show_module_config_dialog(ctx: Any, module_name: str) -> None:
    """Open the modeless standalone config dialog for *module_name*."""
    ModuleConfigDialog.show(ctx, module_name)


def show_vision_settings_dialog(ctx: Any) -> None:
    """Open Vision / OCR settings (vision module config_dialog)."""
    show_module_config_dialog(ctx, "vision")
