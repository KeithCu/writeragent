# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Modeless module config dialogs generated from module.yaml config_dialog specs."""

from __future__ import annotations

import logging
from typing import Any

import unohelper
from com.sun.star.awt import XActionListener, XTopWindowListener

from plugin.chatbot.dialogs import (
    TabListener,
    get_checkbox_state,
    get_control_text,
    get_optional,
    is_checkbox_control,
    set_checkbox_state,
    set_control_text,
    translate_dialog,
)
from plugin.framework.config import as_bool, get_config, parse_float_robust, parse_int_robust, set_config
from plugin.framework.event_bus import global_event_bus
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_extension_url

log = logging.getLogger(__name__)

_active_dialogs: dict[str, Any] = {}

_TAB_PAGE_MAP = {
    "btn_tab_general": 1,
    "btn_tab_ocr": 2,
    "btn_tab_tables": 3,
    "btn_tab_advanced": 4,
}


def _find_module_manifest(module_name: str) -> dict[str, Any] | None:
    try:
        from plugin._manifest import MODULES
    except ImportError:
        return None
    for m in MODULES:
        if not isinstance(m, dict):
            continue
        if str(m.get("name", "")) == module_name:
            return m
    return None


def get_module_config_dialog_id(module_name: str) -> str | None:
    manifest = _find_module_manifest(module_name)
    if not manifest:
        return None
    cfg_dialog = manifest.get("config_dialog") or {}
    dialog_id = str(cfg_dialog.get("id") or "").strip()
    return dialog_id or None


def get_module_config_field_specs(ctx: Any, module_name: str) -> list[dict[str, Any]]:
    """Field specs for a standalone module config dialog (flat control ids)."""
    manifest = _find_module_manifest(module_name)
    if not manifest:
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

        config_key = f"{module_name}.{field_name}"
        val = get_config(ctx, config_key)
        opts = schema.get("options", [])

        if isinstance(opts, list) and opts and isinstance(opts[0], dict):
            for o in opts:
                if isinstance(o, dict) and str(o.get("value", "")) == str(val).strip().lower():
                    val = _(str(o.get("label", val)))
                    break

        field: dict[str, Any] = {"name": field_name, "config_key": config_key, "value": str(val)}

        provider_path = schema.get("options_provider")
        if provider_path and isinstance(provider_path, str):
            from plugin.chatbot.settings_dialog import _call_options_provider

            try:
                field["options"] = _call_options_provider(ctx, provider_path)
            except Exception:
                log.exception("options_provider failed for %s", config_key)
        elif schema.get("options"):
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


def apply_module_config_result(ctx: Any, module_name: str, result: dict[str, Any]) -> None:
    """Persist standalone module dialog values to writeragent.json."""
    field_specs = get_module_config_field_specs(ctx, module_name)
    by_name = {f["name"]: f for f in field_specs}
    int_names = {f["name"] for f in field_specs if f.get("type") == "int"}
    float_names = {f["name"] for f in field_specs if f.get("type") == "float"}

    for key, val in result.items():
        if key not in by_name:
            continue
        spec = by_name[key]
        save_key = str(spec.get("config_key") or f"{module_name}.{key}")

        if key in int_names:
            try:
                val = parse_int_robust(val)
            except ValueError:
                pass
        elif key in float_names:
            try:
                val = parse_float_robust(val)
            except ValueError:
                pass

        if "options" in spec and val:
            for opt in spec["options"]:
                if isinstance(opt, dict):
                    lbl = str(opt.get("label") or opt.get("value") or "")
                    if _(lbl) == str(val):
                        val = opt.get("value", lbl)
                        break

        set_config(ctx, save_key, val)

    global_event_bus.emit("config:changed", ctx=ctx)


def _option_labels(field: dict[str, Any]) -> tuple[str, ...]:
    opts = field.get("options")
    if not isinstance(opts, list):
        return ()
    labels: list[str] = []
    for opt in opts:
        if isinstance(opt, dict):
            labels.append(_(str(opt.get("label") or opt.get("value") or "")))
        elif opt is not None:
            labels.append(_(str(opt)))
    return tuple(labels)


def _set_field_options(ctrl: Any, field: dict[str, Any]) -> None:
    labels = _option_labels(field)
    if not labels:
        log.warning("Module config field %s has no select options", field.get("name"))
        return
    model = ctrl.getModel() if hasattr(ctrl, "getModel") else None
    if model is not None and hasattr(model, "StringItemList"):
        model.StringItemList = labels
        log.debug("Module config set %d options on %s", len(labels), field.get("name"))
        return
    if hasattr(ctrl, "addItem"):
        try:
            while ctrl.getItemCount() > 0:
                ctrl.removeItems(0, 1)
        except Exception:
            pass
        for label in labels:
            ctrl.addItem(label, 0)
        log.debug("Module config addItem populated %d options on %s", len(labels), field.get("name"))
        return
    log.warning("Module config control %s does not support option lists", field.get("name"))


class ModuleConfigDialog:
    """Modeless settings dialog for one MODULES entry with config_dialog metadata."""

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
            base_url = get_extension_url()
            dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", ctx)
            dlg = dp.createDialog(base_url + "/WriterAgentDialogs/%s.xdl" % dialog_id)
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
            def windowClosing(self, e):
                owner.close()

            def windowClosed(self, e):
                pass

            def windowOpened(self, e):
                pass

            def windowMinimized(self, e):
                pass

            def windowNormalized(self, e):
                pass

            def windowActivated(self, e):
                pass

            def windowDeactivated(self, e):
                pass

            def disposing(self, Source):
                pass

        self._top_listener = _TopWindowListener()
        dlg.addTopWindowListener(self._top_listener)
        dlg.setVisible(True)

    def _setup_tabs(self) -> None:
        assert self._dlg is not None
        for tab_id, page_num in _TAB_PAGE_MAP.items():
            btn = get_optional(self._dlg, tab_id)
            if btn is not None:
                btn.addActionListener(TabListener(self._dlg, page_num))

    def _wire_buttons(self) -> None:
        assert self._dlg is not None
        owner = self

        class _ApplyListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                owner._apply(close=False)

            def disposing(self, Source):
                pass

        class _OkListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                owner._apply(close=True)

            def disposing(self, Source):
                pass

        class _CloseListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                owner.close()

            def disposing(self, Source):
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
        assert self._dlg is not None
        for field in field_specs:
            ctrl = self._dlg.getControl(field["name"])
            if ctrl is None:
                log.warning(
                    "Module config dialog %s missing control %r",
                    self._module_name,
                    field["name"],
                )
                continue
            if is_checkbox_control(ctrl):
                set_checkbox_state(ctrl, 1 if as_bool(field["value"]) else 0)
            elif hasattr(ctrl, "setText"):
                if "options" in field:
                    try:
                        _set_field_options(ctrl, field)
                    except Exception:
                        log.exception("Failed to set options for %s", field["name"])
                ctrl.setText(str(field.get("value", "")))
            else:
                if "options" in field:
                    try:
                        _set_field_options(ctrl, field)
                    except Exception:
                        log.exception("Failed to set options for %s", field["name"])
                set_control_text(ctrl, field["value"])

    def _extract_result(self) -> dict[str, Any]:
        assert self._dlg is not None
        result: dict[str, Any] = {}
        for field in get_module_config_field_specs(self._ctx, self._module_name):
            name = field["name"]
            ctrl = self._dlg.getControl(name)
            if ctrl is None:
                continue
            if is_checkbox_control(ctrl):
                result[name] = "true" if get_checkbox_state(ctrl) else "false"
            elif hasattr(ctrl, "getText"):
                result[name] = ctrl.getText()
            else:
                result[name] = get_control_text(ctrl)
        return result

    def _apply(self, *, close: bool) -> None:
        try:
            result = self._extract_result()
            apply_module_config_result(self._ctx, self._module_name, result)
        except Exception:
            log.exception("Failed to apply module config for %s", self._module_name)
        if close:
            self.close()


def show_module_config_dialog(ctx: Any, module_name: str) -> None:
    """Open the modeless standalone config dialog for *module_name*."""
    ModuleConfigDialog.show(ctx, module_name)


def show_vision_settings_dialog(ctx: Any) -> None:
    """Open Vision / OCR settings (vision module config_dialog)."""
    show_module_config_dialog(ctx, "vision")
