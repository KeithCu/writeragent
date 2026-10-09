# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Modeless module config dialogs generated from module.yaml config_dialog specs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import unohelper
from com.sun.star.awt import XActionListener, XTopWindowListener

if TYPE_CHECKING:
    from com.sun.star.awt import ActionEvent
    from com.sun.star.lang import EventObject

from plugin.chatbot.dialogs import TabListener, get_optional, translate_dialog
from plugin.framework.uno_context import get_extension_url

log = logging.getLogger(__name__)

_active_dialogs: dict[str, Any] = {}

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
    def show(cls, ctx: Any, module_name: str) -> None:
        existing = _active_dialogs.get(module_name)
        if existing is not None:
            try:
                existing.close()
            except Exception:
                log.debug("Failed to close prior module config dialog", exc_info=True)
        dialog = cls(ctx, module_name)
        _active_dialogs[module_name] = dialog
        dialog._open()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _active_dialogs.pop(self._module_name, None)
        dlg = self._dlg
        self._dlg = None
        if dlg is None:
            return
        try:
            dlg.setVisible(False)
        except Exception:
            log.exception("Failed to hide module config dialog")
        try:
            dlg.dispose()
        except Exception:
            log.exception("Failed to dispose module config dialog")

    def _open(self) -> None:
        ctx = self._ctx
        dialog_id = get_module_config_dialog_id(self._module_name)
        if not dialog_id:
            log.error("No config_dialog.id for module %s", self._module_name)
            return

        try:
            smgr = ctx.getServiceManager()
            base_url = get_extension_url(ctx)
            dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", ctx)
            dlg = dp.createDialog(base_url + "/Dialogs/%s.xdl" % dialog_id)
        except Exception:
            log.exception("Failed to load module config dialog %s", dialog_id)
            return

        self._dlg = dlg
        translate_dialog(dlg)
        self._setup_tabs()
        self._wire_buttons()
        self._populate_fields(get_module_config_field_specs(ctx, self._module_name))

        owner = self

        class _TopWindowListener(unohelper.Base, XTopWindowListener):
            def windowClosing(self, e: EventObject) -> None:
                owner.close()

            def windowClosed(self, e: EventObject) -> None:
                pass

            def windowOpened(self, e: EventObject) -> None:
                pass

            def windowMinimized(self, e: EventObject) -> None:
                pass

            def windowNormalized(self, e: EventObject) -> None:
                pass

            def windowActivated(self, e: EventObject) -> None:
                pass

            def windowDeactivated(self, e: EventObject) -> None:
                pass

            def disposing(self, Source: EventObject) -> None:
                pass

        self._top_listener = _TopWindowListener()
        dlg.addTopWindowListener(self._top_listener)
        dlg.setVisible(True)

    def _setup_tabs(self) -> None:
        assert self._dlg is not None
        # Step numbers follow control order, which is the page order the
        # generator writes. A new page: in module.yaml then gets a listener
        # without a second hardcoded map.
        for page_num, btn in enumerate(_tab_buttons_in_order(self._dlg), start=1):
            btn.addActionListener(TabListener(self._dlg, page_num))

    def _wire_buttons(self) -> None:
        assert self._dlg is not None
        owner = self

        class _ApplyListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: ActionEvent) -> None:
                owner._apply(close=False)

            def disposing(self, Source: EventObject) -> None:
                pass

        class _OkListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: ActionEvent) -> None:
                owner._apply(close=True)

            def disposing(self, Source: EventObject) -> None:
                pass

        class _CloseListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: ActionEvent) -> None:
                owner.close()

            def disposing(self, Source: EventObject) -> None:
                pass

        apply_btn = get_optional(self._dlg, "btn_apply")
        if apply_btn:
            apply_btn.addActionListener(_ApplyListener())
        ok_btn = get_optional(self._dlg, "btn_ok")
        if ok_btn:
            ok_btn.addActionListener(_OkListener())
        close_btn = get_optional(self._dlg, "btn_close")
        if close_btn:
            close_btn.addActionListener(_CloseListener())

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
