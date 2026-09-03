# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Steerable slash-command completion menu attached to the sidebar Ask field.

A native ``PopupMenu`` is modal — the user could not keep typing. The XDL
placeholder is ``dlg:menulist`` (LibreOffice dialog.dtd has no ``listbox``;
a ``listbox`` tag can crash the sidebar). That control is a ComboBox — one
line plus a dropdown — so it does not look like a completion menu. This
controller inserts a real ``UnoControlListBox`` on the dialog and parks it
just above (or below) the Ask field while the user types.
"""

from __future__ import annotations

import logging
from typing import Any

from plugin.chatbot.dialogs import set_control_text, set_control_visible
from plugin.chatbot.slash_commands import (
    SLASH_COMMANDS,
    SlashCommand,
    classify_slash_key,
    filter_slash_commands,
    format_slash_item,
    load_slash_lru,
    run_slash_command,
    slash_typed_prefix,
)
from plugin.framework.errors import suppress_disposed

log = logging.getLogger("writeragent.slash_popup")

_POPUP_MAX_ROWS = 6
_POPUP_ROW_PX = 14
_POS_SIZE_FLAGS = 15  # X + Y + WIDTH + HEIGHT
_RUNTIME_LIST_NAME = "slash_popup_list"


def _supported_services(ctrl: Any) -> str:
    try:
        model = ctrl.getModel() if ctrl is not None else None
        if model is not None and hasattr(model, "getSupportedServiceNames"):
            return " ".join(str(s) for s in model.getSupportedServiceNames())
    except Exception:
        return ""
    return ""


def _is_combo_box(ctrl: Any) -> bool:
    """True for the XDL ``menulist`` placeholder (ComboBox), not a ListBox."""
    names = _supported_services(ctrl)
    return "ComboBox" in names or "UnoControlComboBox" in names


def _dialog_for(ctrl: Any) -> Any:
    cur = ctrl
    hops = 0
    while cur is not None and hops < 8:
        hops += 1
        get_model = getattr(cur, "getModel", None)
        if callable(get_model) and hasattr(cur, "getControl"):
            model = None
            try:
                model = get_model()
            except Exception:
                model = None
            if model is not None and hasattr(model, "insertByName"):
                return cur
        getter = getattr(cur, "getContext", None) or getattr(cur, "getPeer", None)
        try:
            cur = getter() if callable(getter) else None
        except Exception:
            return None
    return None


def _ensure_listbox(control: Any, query_control: Any) -> Any:
    """Replace the XDL ComboBox placeholder with a multi-row ListBox."""
    if control is not None and not _is_combo_box(control):
        return control
    dlg = _dialog_for(query_control) or _dialog_for(control)
    if dlg is None:
        log.warning("slash popup: no dialog to insert ListBox; using XDL control")
        return control
    try:
        if hasattr(dlg, "getControl"):
            existing = dlg.getControl(_RUNTIME_LIST_NAME)
            if existing is not None:
                set_control_visible(control, False)
                return existing
    except Exception:
        existing = None
    model = dlg.getModel()
    list_model = model.createInstance("com.sun.star.awt.UnoControlListBoxModel")
    list_model.Name = _RUNTIME_LIST_NAME
    list_model.Dropdown = False
    list_model.Tabstop = False
    list_model.Border = 1
    try:
        list_model.MultiSelection = False
    except Exception:
        pass
    qr = query_control.getPosSize() if query_control is not None and hasattr(query_control, "getPosSize") else None
    if qr is not None:
        list_model.PositionX = int(qr.X)
        list_model.PositionY = max(16, int(qr.Y) - 90)
        list_model.Width = int(qr.Width)
        list_model.Height = 80
    else:
        list_model.PositionX = 4
        list_model.PositionY = 80
        list_model.Width = 142
        list_model.Height = 60
    model.insertByName(_RUNTIME_LIST_NAME, list_model)
    created = dlg.getControl(_RUNTIME_LIST_NAME)
    if created is None:
        log.warning("slash popup: ListBox insertByName succeeded but getControl is None")
        return control
    set_control_visible(control, False)
    log.info("slash popup: runtime ListBox attached (XDL menulist is ComboBox)")
    return created


class SlashPopupController:
    """Show, filter, and accept slash commands on the Ask-field ListBox."""

    def __init__(self, control: Any, send_listener: Any, query_control: Any) -> None:
        self.query_control = query_control
        self.send_listener = send_listener
        self.control = _ensure_listbox(control, query_control)
        self._open = False
        self._matches: list[SlashCommand] = []
        self._selected = 0
        self.hide()
        self._attach_click()
        self._attach_keys()

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def visible_names(self) -> list[str]:
        return [cmd.name for cmd in self._matches]

    @property
    def selected_name(self) -> str | None:
        if not self._matches or not (0 <= self._selected < len(self._matches)):
            return None
        return self._matches[self._selected].name

    def hide(self) -> None:
        self._open = False
        self._matches = []
        self._selected = 0
        set_control_visible(self.control, False)

    def on_query_text(self, text: str) -> None:
        """Open or narrow the popup from the current Ask-box contents."""
        if slash_typed_prefix(text) is None:
            self.hide()
            return
        matches = filter_slash_commands(text, load_slash_lru(), SLASH_COMMANDS)
        if not matches:
            self.hide()
            return
        self._matches = matches
        self._selected = 0
        self._refresh_list()
        self._open = True
        self.reposition()
        set_control_visible(self.control, True)
        # ListBox can steal focus; Esc/arrows must stay on the Ask field.
        set_focus = getattr(self.query_control, "setFocus", None)
        if callable(set_focus):
            with suppress_disposed("slash popup return focus", logger=log):
                set_focus()

    def handle_key(self, key_code: int, modifiers: int = 0) -> bool:
        """True when the popup consumed the key (do not Send / insert newline)."""
        if not self._open:
            return False
        action = classify_slash_key(key_code, modifiers)
        if action is None:
            return False
        if action == "escape":
            self.hide()
            return True
        if action == "up":
            self.move_selection(-1)
            return True
        if action == "down":
            self.move_selection(1)
            return True
        if action == "tab":
            self.complete_selected()
            return True
        if action == "enter":
            self.accept_selected()
            return True
        return False

    def move_selection(self, delta: int) -> None:
        if not self._matches:
            return
        self._selected = (self._selected + delta) % len(self._matches)
        self._select_row(self._selected)

    def complete_selected(self) -> None:
        name = self.selected_name
        if not name:
            return
        set_control_text(self.query_control, "/" + name)
        self.on_query_text("/" + name)

    def accept_selected(self) -> None:
        name = self._selected_name_from_control()
        if not name:
            return
        run_slash_command(name, self.send_listener)

    def reposition(self) -> None:
        """Park the list just above the Ask field; fall back below if no room."""
        query = self.query_control
        ctrl = self.control
        if query is None or ctrl is None or not hasattr(query, "getPosSize"):
            return
        with suppress_disposed("slash popup reposition", logger=log):
            qr = query.getPosSize()
            rows = min(len(self._matches) or 1, _POPUP_MAX_ROWS)
            height = max(24, rows * _POPUP_ROW_PX + 6)
            above_y = int(qr.Y) - height - 2
            y = above_y if above_y >= 16 else int(qr.Y) + int(qr.Height) + 2
            ctrl.setPosSize(int(qr.X), y, int(qr.Width), height, _POS_SIZE_FLAGS)

    def _selected_name_from_control(self) -> str | None:
        ctrl = self.control
        if ctrl is not None and hasattr(ctrl, "getSelectedItemPos"):
            try:
                pos = int(ctrl.getSelectedItemPos())
                if 0 <= pos < len(self._matches):
                    self._selected = pos
            except Exception:
                pass
        return self.selected_name

    def _refresh_list(self) -> None:
        ctrl = self.control
        if ctrl is None:
            return
        labels = tuple(format_slash_item(cmd) for cmd in self._matches)
        with suppress_disposed("slash popup refresh", logger=log):
            if hasattr(ctrl, "getItemCount") and hasattr(ctrl, "removeItems"):
                count = int(ctrl.getItemCount() or 0)
                if count:
                    ctrl.removeItems(0, count)
            if labels and hasattr(ctrl, "addItems"):
                ctrl.addItems(labels, 0)
            self._select_row(0)

    def _select_row(self, idx: int) -> None:
        ctrl = self.control
        if ctrl is None or not self._matches:
            return
        idx = max(0, min(idx, len(self._matches) - 1))
        self._selected = idx
        if hasattr(ctrl, "selectItemPos"):
            with suppress_disposed("slash popup select", logger=log):
                ctrl.selectItemPos(idx, True)

    def _attach_click(self) -> None:
        ctrl = self.control
        if ctrl is None or not hasattr(ctrl, "addMouseListener"):
            return
        try:
            import unohelper
            from com.sun.star.awt import XMouseListener
        except ImportError:
            return

        host = self

        class _Click(unohelper.Base, XMouseListener):  # type: ignore[misc]
            def disposing(self, Source):  # noqa: N802, N803 -- UNO signature
                return

            def mousePressed(self, e):  # noqa: N802 -- UNO signature
                return

            def mouseReleased(self, e):  # noqa: N802 -- UNO signature
                if host.is_open:
                    host.accept_selected()

            def mouseEntered(self, e):  # noqa: N802 -- UNO signature
                return

            def mouseExited(self, e):  # noqa: N802 -- UNO signature
                return

        try:
            ctrl.addMouseListener(_Click())
        except Exception:
            log.debug("slash popup mouse listener attach failed", exc_info=True)

    def _attach_keys(self) -> None:
        """Forward Esc/Enter/arrows when the ListBox stole focus from Ask."""
        ctrl = self.control
        if ctrl is None or not hasattr(ctrl, "addKeyListener"):
            return
        try:
            import unohelper
            from com.sun.star.awt import XKeyListener
        except ImportError:
            return

        host = self

        class _Keys(unohelper.Base, XKeyListener):  # type: ignore[misc]
            def disposing(self, Source):  # noqa: N802, N803 -- UNO signature
                return

            def keyPressed(self, e):  # noqa: N802 -- UNO signature
                if host.handle_key(int(getattr(e, "KeyCode", 0) or 0), int(getattr(e, "Modifiers", 0) or 0)):
                    with suppress_disposed("slash list Consume", logger=log):
                        if hasattr(e, "Consume"):
                            setattr(e, "Consume", True)
                return

            def keyReleased(self, e):  # noqa: N802 -- UNO signature
                return

        try:
            ctrl.addKeyListener(_Keys())
        except Exception:
            log.debug("slash popup key listener attach failed", exc_info=True)
