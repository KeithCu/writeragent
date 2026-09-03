# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Steerable slash-command completion menu attached to the sidebar Ask field.

A native ``PopupMenu`` is modal — the user could not keep typing. The XDL
placeholder is ``dlg:menulist`` (LibreOffice dialog.dtd has no ``listbox``;
a ``listbox`` tag can crash the sidebar). That control is a ComboBox — one
line plus a dropdown — so it does not look like a completion menu.

PR 564 inserted a runtime ``UnoControlListBox`` with ``Dropdown=False`` and
``setPosSize`` just above Ask. Sibling dialog controls do not paint over the
rich-text transcript, so the list was crushed into the Ready/Ask gap. This
controller still inserts a ListBox (same key contract) but sets
``Dropdown=True`` so VCL draws its own floating dropdown window — a real
overlay — and parks only a one-line closed combo in the transcript, not a
multi-row list in the bottom band.
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
# Closed dropdown combo is one line. The item list is VCL's overlay window.
_POPUP_CLOSED_PX = 14
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


def _control_model(ctrl: Any) -> Any:
    get_model = getattr(ctrl, "getModel", None) if ctrl is not None else None
    if callable(get_model):
        try:
            return get_model()
        except Exception:
            return None
    return None


def _apply_dropdown_model(list_model: Any) -> None:
    """Mark the ListBox as a VCL dropdown overlay, not an in-layout multi-row list."""
    if list_model is None:
        return
    list_model.Dropdown = True
    if hasattr(list_model, "LineCount"):
        list_model.LineCount = _POPUP_MAX_ROWS
    list_model.Tabstop = False
    if hasattr(list_model, "Border"):
        list_model.Border = 1
    try:
        list_model.MultiSelection = False
    except Exception:
        pass


def is_dropdown_overlay(ctrl: Any) -> bool:
    """True when the completion control uses VCL's dropdown window."""
    model = _control_model(ctrl)
    if model is None:
        model = ctrl
    return bool(getattr(model, "Dropdown", False))


def _apply_dropdown_overlay(ctrl: Any) -> None:
    """Force Dropdown=True on an already-created ListBox (tests + reused runtime)."""
    model = _control_model(ctrl)
    if model is not None:
        _apply_dropdown_model(model)
    if ctrl is not None and hasattr(ctrl, "setDropDownLineCount"):
        try:
            ctrl.setDropDownLineCount(_POPUP_MAX_ROWS)
        except Exception:
            log.debug("slash popup setDropDownLineCount failed", exc_info=True)


def _toggle_list_popup(ctrl: Any) -> bool:
    """Open or close the VCL dropdown via the ListBox accessible ``togglePopup`` action."""
    if ctrl is None or not hasattr(ctrl, "getAccessibleContext"):
        return False
    try:
        acc = ctrl.getAccessibleContext()
    except Exception:
        return False
    if acc is None or not hasattr(acc, "doAccessibleAction"):
        return False
    try:
        count = int(acc.getAccessibleActionCount() or 0) if hasattr(acc, "getAccessibleActionCount") else 0
        for idx in range(count):
            desc = ""
            if hasattr(acc, "getAccessibleActionDescription"):
                desc = str(acc.getAccessibleActionDescription(idx) or "").lower()
            if "popup" in desc or "dropdown" in desc or count == 1:
                return bool(acc.doAccessibleAction(idx))
    except Exception:
        log.debug("slash popup togglePopup failed", exc_info=True)
    return False


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
    """Replace the XDL ComboBox placeholder with a dropdown ListBox overlay."""
    if control is not None and not _is_combo_box(control):
        _apply_dropdown_overlay(control)
        return control
    dlg = _dialog_for(query_control) or _dialog_for(control)
    if dlg is None:
        log.warning("slash popup: no dialog to insert ListBox; using XDL control")
        _apply_dropdown_overlay(control)
        return control
    try:
        if hasattr(dlg, "getControl"):
            existing = dlg.getControl(_RUNTIME_LIST_NAME)
            if existing is not None:
                set_control_visible(control, False)
                _apply_dropdown_overlay(existing)
                return existing
    except Exception:
        existing = None
    model = dlg.getModel()
    list_model = model.createInstance("com.sun.star.awt.UnoControlListBoxModel")
    list_model.Name = _RUNTIME_LIST_NAME
    _apply_dropdown_model(list_model)
    qr = query_control.getPosSize() if query_control is not None and hasattr(query_control, "getPosSize") else None
    if qr is not None:
        list_model.PositionX = int(qr.X)
        # Closed combo only. Park in the transcript so VCL's downward
        # dropdown covers Ready / history instead of the Ready/Ask gap.
        list_model.PositionY = max(16, int(qr.Y) - _overlay_height(_POPUP_MAX_ROWS) - _POPUP_CLOSED_PX - 2)
        list_model.Width = int(qr.Width)
        list_model.Height = _POPUP_CLOSED_PX
    else:
        list_model.PositionX = 4
        list_model.PositionY = 80
        list_model.Width = 142
        list_model.Height = _POPUP_CLOSED_PX
    model.insertByName(_RUNTIME_LIST_NAME, list_model)
    created = dlg.getControl(_RUNTIME_LIST_NAME)
    if created is None:
        log.warning("slash popup: ListBox insertByName succeeded but getControl is None")
        _apply_dropdown_overlay(control)
        return control
    set_control_visible(control, False)
    log.info("slash popup: runtime dropdown ListBox attached (VCL overlay)")
    return created


def _overlay_height(rows: int) -> int:
    return max(24, min(rows, _POPUP_MAX_ROWS) * _POPUP_ROW_PX + 6)


class SlashPopupController:
    """Show, filter, and accept slash commands on the Ask-field ListBox."""

    def __init__(self, control: Any, send_listener: Any, query_control: Any) -> None:
        self.query_control = query_control
        self.send_listener = send_listener
        self.control = _ensure_listbox(control, query_control)
        self._open = False
        self._overlay_open = False
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
        self._close_overlay()
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
        self._open_overlay()
        # Dropdown can steal focus; Esc/arrows / further typing must stay on Ask.
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
        """Park the one-line closed combo in the transcript, not the Ready/Ask gap.

        The visible menu is VCL's dropdown window (``Dropdown=True``). The closed
        combo is only an anchor: sit it ``overlay_height`` above Ask so a
        downward dropdown covers Ready / lower history instead of squeezing
        into leftover layout space.
        """
        query = self.query_control
        ctrl = self.control
        if query is None or ctrl is None or not hasattr(query, "getPosSize"):
            return
        with suppress_disposed("slash popup reposition", logger=log):
            qr = query.getPosSize()
            rows = min(len(self._matches) or 1, _POPUP_MAX_ROWS)
            if hasattr(ctrl, "setDropDownLineCount"):
                try:
                    ctrl.setDropDownLineCount(rows)
                except Exception:
                    log.debug("slash popup setDropDownLineCount failed", exc_info=True)
            model = _control_model(ctrl)
            if model is not None and hasattr(model, "LineCount"):
                try:
                    model.LineCount = rows
                except Exception:
                    pass
            # Closed height stays one line — never the multi-row in-layout size
            # that collapsed into the Ready/Ask strip (Keith screenshot).
            closed_h = _POPUP_CLOSED_PX
            y = int(qr.Y) - _overlay_height(rows) - closed_h - 2
            if y < 16:
                y = 16
            ctrl.setPosSize(int(qr.X), y, int(qr.Width), closed_h, _POS_SIZE_FLAGS)

    def _open_overlay(self) -> None:
        """Drop the VCL list if it is not already the floating overlay."""
        if self._overlay_open:
            return
        if _toggle_list_popup(self.control):
            self._overlay_open = True

    def _close_overlay(self) -> None:
        if not self._overlay_open:
            return
        _toggle_list_popup(self.control)
        self._overlay_open = False

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
