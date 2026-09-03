# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Steerable slash-command completion menu attached to the sidebar Ask field.

A native ``PopupMenu`` is modal — the user could not keep typing. The XDL
placeholder is ``dlg:menulist`` (LibreOffice dialog.dtd has no ``listbox``).

PR 564 parked an in-dialog ListBox in the Ready/Ask gap (siblings cannot
paint over the rich-text transcript). PR 575 tried ``Dropdown=True`` plus
accessible ``togglePopup`` so VCL would drop a floating list. Headed
sign-off: typing ``/`` left a one-line closed combo in the transcript
(``togglePopup`` did not open), and clicking the combo arrow ran ``/help``
into the chat. This controller hides that placeholder and shows a toolkit
``listbox`` window parented to the Ask field's peer — a tall overlay above
Ask, opened by the slash prefix, not by a click.
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


def _overlay_height(rows: int) -> int:
    return max(24, min(max(rows, 1), _POPUP_MAX_ROWS) * _POPUP_ROW_PX + 6)


def _popup_bounds(query_width: int, rows: int) -> tuple[int, int, int, int]:
    """Ask-peer-relative rectangle: tall list just above the field (Y negative)."""
    height = _overlay_height(rows)
    return (0, -height - 2, max(20, int(query_width)), height)


def _row_index_at_y(y: int, n_rows: int) -> int | None:
    """List row under a mouse Y, or None when the click is not on a row."""
    if y < 0 or n_rows <= 0:
        return None
    row = int(y) // _POPUP_ROW_PX
    if 0 <= row < n_rows:
        return row
    return None


def uses_toolkit_overlay(popup: Any) -> bool:
    """True when the visible menu is a toolkit window, not an in-dialog control."""
    return getattr(popup, "_popup_window", None) is not None


def _query_peer(query_control: Any) -> Any:
    get_peer = getattr(query_control, "getPeer", None)
    if not callable(get_peer):
        return None
    try:
        return get_peer()
    except Exception:
        return None


def _toolkit_for_peer(peer: Any) -> Any:
    if peer is not None and hasattr(peer, "getToolkit"):
        try:
            toolkit = peer.getToolkit()
            if toolkit is not None:
                return toolkit
        except Exception:
            pass
    return None


def _awt_window_constants() -> tuple[Any, Any, int, int, int] | None:
    """WindowClass / WindowAttribute as IDL integers (enum modules are untyped)."""
    try:
        from com.sun.star.awt import Rectangle, WindowDescriptor
    except ImportError:
        return None
    # com.sun.star.awt.WindowClass: TOP=0, SIMPLE=3
    # com.sun.star.awt.WindowAttribute: SHOW=1, BORDER=16
    return Rectangle, WindowDescriptor, 0, 3, 1 | 16


def _create_ask_peer_listbox(query_control: Any) -> Any:
    """Native VCL ListBox window parented to Ask. None when there is no peer (unit tests)."""
    parent = _query_peer(query_control)
    if parent is None:
        return None
    toolkit = _toolkit_for_peer(parent)
    if toolkit is None or not hasattr(toolkit, "createWindow"):
        return None
    consts = _awt_window_constants()
    if consts is None:
        return None
    rectangle_cls, descriptor_cls, top, simple, attrs = consts
    qr = query_control.getPosSize() if hasattr(query_control, "getPosSize") else None
    width = int(qr.Width) if qr is not None else 142
    x, y, w, h = _popup_bounds(width, _POPUP_MAX_ROWS)
    # TOP/listbox floats over the rich-text transcript. SIMPLE is the clip fallback.
    for wtype in (top, simple):
        desc = descriptor_cls()
        desc.Type = wtype
        desc.WindowServiceName = "listbox"
        desc.Parent = parent
        desc.ParentIndex = 1
        desc.Bounds = rectangle_cls(x, y, w, h)
        desc.WindowAttributes = attrs
        try:
            win = toolkit.createWindow(desc)
        except Exception:
            log.debug("slash popup: createWindow %s/listbox failed", wtype, exc_info=True)
            continue
        if win is None:
            continue
        set_control_visible(win, False)
        log.info("slash popup: toolkit listbox overlay parented to Ask peer (%s)", wtype)
        return win
    log.warning("slash popup: toolkit listbox overlay could not be created")
    return None


class SlashPopupController:
    """Show, filter, and accept slash commands on the Ask-field overlay list."""

    def __init__(self, control: Any, send_listener: Any, query_control: Any) -> None:
        self.query_control = query_control
        self.send_listener = send_listener
        self._placeholder = control
        # Never leave the XDL menulist / closed combo visible in the transcript.
        set_control_visible(control, False)
        self._popup_window: Any = None
        self.control: Any = None
        self._open = False
        self._matches: list[SlashCommand] = []
        self._selected = 0
        self._bind_overlay()
        if self.control is None and control is not None and not _is_combo_box(control):
            # Unit tests pass a mock list (no Ask peer / toolkit).
            self.control = control
        self.hide()
        self._attach_click()
        self._attach_keys()

    def _bind_overlay(self) -> None:
        if self._popup_window is not None:
            return
        win = _create_ask_peer_listbox(self.query_control)
        if win is None:
            return
        self._popup_window = win
        self.control = win

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
        set_control_visible(self._placeholder, False)

    def on_query_text(self, text: str) -> None:
        """Open or narrow the popup from the current Ask-box contents."""
        if slash_typed_prefix(text) is None:
            self.hide()
            return
        matches = filter_slash_commands(text, load_slash_lru(), SLASH_COMMANDS)
        if not matches:
            self.hide()
            return
        self._bind_overlay()
        if self.control is None:
            log.warning("slash popup: no overlay list; slash prefix ignored")
            return
        self._matches = matches
        self._selected = 0
        self._refresh_list()
        self._open = True
        self.reposition()
        # Hide the XDL/closed combo every show — it is not the menu.
        set_control_visible(self._placeholder, False)
        set_control_visible(self.control, True)
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

    def accept_row_at_y(self, y: int) -> bool:
        """Accept the command under mouse Y. Ignores chrome / off-list clicks."""
        row = _row_index_at_y(y, len(self._matches))
        if row is None or not self._open:
            return False
        self._selected = row
        self._select_row(row)
        self.accept_selected()
        return True

    def reposition(self) -> None:
        """Size the overlay to the match rows and park it above Ask."""
        query = self.query_control
        ctrl = self.control
        if query is None or ctrl is None or not hasattr(query, "getPosSize"):
            return
        with suppress_disposed("slash popup reposition", logger=log):
            qr = query.getPosSize()
            rows = min(len(self._matches) or 1, _POPUP_MAX_ROWS)
            x, y, w, h = _popup_bounds(int(qr.Width), rows)
            if self._popup_window is not None:
                # Coordinates are relative to the Ask peer: Y is above the field.
                ctrl.setPosSize(x, y, w, h, _POS_SIZE_FLAGS)
                return
            # Mock list in unit tests: tall rectangle above Ask, never 14px closed.
            above_y = int(qr.Y) + y
            if above_y < 16:
                above_y = 16
            ctrl.setPosSize(int(qr.X), above_y, w, h, _POS_SIZE_FLAGS)

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
                # Row click only. A closed-combo arrow used to fire this with
                # /help already selected and dump the help list into chat.
                host.accept_row_at_y(int(getattr(e, "Y", -1) or -1))

            def mouseEntered(self, e):  # noqa: N802 -- UNO signature
                return

            def mouseExited(self, e):  # noqa: N802 -- UNO signature
                return

        try:
            ctrl.addMouseListener(_Click())
        except Exception:
            log.debug("slash popup mouse listener attach failed", exc_info=True)

    def _attach_keys(self) -> None:
        """Forward Esc/Enter/arrows when the list stole focus from Ask."""
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
