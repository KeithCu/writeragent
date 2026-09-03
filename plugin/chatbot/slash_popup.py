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

def _ovlog(msg: str, *args: object, exc_info: bool = False) -> None:
    """Slash-overlay breadcrumb. Logger only — no /tmp (Bandit B108)."""
    text = (msg % args) if args else msg
    log.info("[SLASH-OV] %s", text, exc_info=exc_info)


def _ovdiag(obj: Any, label: str) -> None:
    """Peer/window geometry + visibility for headed overlay debugging."""
    if obj is None:
        _ovlog("%s None", label)
        return
    bits = [label, "py=%s" % type(obj).__name__]
    try:
        impl = obj.getImplementationName()
        bits.append("impl=%s" % impl)
    except Exception:
        pass
    try:
        ps = obj.getPosSize()
        bits.append("pos=%sx%s@%s,%s" % (ps.Width, ps.Height, ps.X, ps.Y))
    except Exception as e:
        bits.append("pos_err=%s" % e)
    for name in ("isVisible", "isReallyVisible"):
        fn = getattr(obj, name, None)
        if callable(fn):
            try:
                bits.append("%s=%s" % (name, fn()))
            except Exception as e:
                bits.append("%s_err=%s" % (name, e))
    to_front = getattr(obj, "toFront", None)
    bits.append("toFront=%s" % callable(to_front))
    set_vis = getattr(obj, "setVisible", None)
    bits.append("setVisible=%s" % callable(set_vis))
    try:
        bits.append("output=%sx%s" % (obj.getOutputWidth(), obj.getOutputHeight()))
    except Exception:
        pass
    _ovlog("%s", " ".join(str(b) for b in bits))


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


def _location_on_screen(obj: Any) -> tuple[int, int] | None:
    """Screen origin of an AWT window/control, or None."""
    if obj is None:
        return None
    get_acc = getattr(obj, "getAccessibleContext", None)
    acc = None
    if callable(get_acc):
        try:
            acc = get_acc()
        except Exception:
            acc = None
    if acc is None:
        get_acc = getattr(obj, "getAccessibleContext", None)
    fn = getattr(acc, "getLocationOnScreen", None) if acc is not None else None
    if not callable(fn):
        return None
    try:
        pt = fn()
        # UNO Point is untyped here; getattr avoids basedpyright treating it as object.
        x = getattr(pt, "X", None)
        y = getattr(pt, "Y", None)
        if x is None or y is None:
            return None
        return int(x), int(y)
    except Exception:
        return None


def _overlay_rect(qr: Any, rows: int, *, relative_to_ask: bool) -> tuple[int, int, int, int]:
    """List rectangle. Ask-relative Y is negative (clips in a 49px peer). Dialog-relative sits above Ask."""
    width = max(20, int(getattr(qr, "Width", 0) or 0))
    if relative_to_ask:
        return _popup_bounds(width, rows)
    height = _overlay_height(rows)
    return (int(qr.X), int(qr.Y) - height - 2, width, height)


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


def _query_peer(query_control: Any, label: str = "Ask") -> Any:
    get_peer = getattr(query_control, "getPeer", None)
    if not callable(get_peer):
        _ovlog("%s getPeer missing query=%s", label, type(query_control).__name__)
        return None
    try:
        peer = get_peer()
    except Exception:
        _ovlog("%s getPeer failed", label, exc_info=True)
        return None
    _ovdiag(query_control, "%s control" % label)
    _ovdiag(peer, "%s peer" % label)
    cur = peer
    for i in range(6):
        nxt = None
        for name in ("getParent", "getParentWindow"):
            fn = getattr(cur, name, None)
            if callable(fn):
                try:
                    nxt = fn()
                    break
                except Exception as e:
                    _ovlog("%s ancestor[%s] %s failed: %s", label, i, name, e)
        if nxt is None:
            _ovlog("%s ancestor[%s] no getParent", label, i)
            break
        _ovdiag(nxt, "%s ancestor[%s]" % (label, i))
        cur = nxt
    return peer


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


def _create_ask_peer_listbox(query_control: Any, parent_control: Any = None) -> Any:
    """Native VCL ListBox. Headed: Ask peer is ~49px; Y=-92 is clipped. Parent the dialog instead."""
    ask_peer = _query_peer(query_control)
    parent = ask_peer
    relative_to_ask = True
    if parent_control is not None:
        overlay_peer = _query_peer(parent_control, label="dialog")
        if overlay_peer is not None:
            parent = overlay_peer
            relative_to_ask = overlay_peer is ask_peer
            _ovdiag(overlay_peer, "dialog/overlay parent peer")
            _ovlog("relative_to_ask=%s", relative_to_ask)
    if parent is None:
        return None
    toolkit = _toolkit_for_peer(parent)
    if toolkit is None or not hasattr(toolkit, "createWindow"):
        _ovlog("no toolkit on overlay parent")
        return None
    consts = _awt_window_constants()
    if consts is None:
        return None
    rectangle_cls, descriptor_cls, top, simple, attrs = consts
    qr = query_control.getPosSize() if hasattr(query_control, "getPosSize") else None
    if qr is None:
        qr = type("R", (), {"X": 0, "Y": 0, "Width": 142, "Height": 30})()
    x, y, w, h = _overlay_rect(qr, _POPUP_MAX_ROWS, relative_to_ask=relative_to_ask)
    screen = _location_on_screen(query_control) or _location_on_screen(ask_peer)
    _ovlog("Ask screen=%s dialog_rect=%s,%s %sx%s", screen, x, y, w, h)
    if screen is not None:
        # TOP "window" Bounds are screen coords (8,327 landed on the document).
        x, y = int(screen[0]), int(screen[1]) - h - 2
        _ovlog("window screen bounds=%s,%s %sx%s parent=dialog", x, y, w, h)
    # "window" TOP overlays the transcript without floatingwindow's Escape grab.
    host_desc = descriptor_cls()
    host_desc.Type = top
    host_desc.WindowServiceName = "window"
    host_desc.Parent = parent
    host_desc.ParentIndex = -1
    host_desc.Bounds = rectangle_cls(x, y, w, h)
    host_desc.WindowAttributes = attrs
    _ovlog("createWindow window bounds=%s,%s %sx%s", x, y, w, h)
    try:
        host = toolkit.createWindow(host_desc)
    except Exception:
        _ovlog("createWindow window FAILED", exc_info=True)
        host = None
    if host is None:
        log.warning("slash popup: overlay window could not be created")
        return None
    _ovdiag(host, "overlay-window")
    desc = descriptor_cls()
    desc.Type = simple
    desc.WindowServiceName = "listbox"
    desc.Parent = host
    desc.ParentIndex = -1
    desc.Bounds = rectangle_cls(0, 0, w, h)
    desc.WindowAttributes = attrs
    _ovlog("createWindow listbox in window %sx%s", w, h)
    try:
        win = toolkit.createWindow(desc)
    except Exception:
        _ovlog("createWindow listbox FAILED", exc_info=True)
        win = None
    if win is None:
        log.warning("slash popup: listbox in overlay window could not be created")
        return None
    _ovdiag(win, "listbox-in-window")
    return win, host

    log.warning("slash popup: toolkit listbox overlay could not be created")
    return None


class SlashPopupController:
    """Show, filter, and accept slash commands on the Ask-field overlay list."""

    def __init__(
        self,
        control: Any,
        send_listener: Any,
        query_control: Any,
        overlay_parent: Any = None,
    ) -> None:
        self.query_control = query_control
        self.send_listener = send_listener
        self._overlay_parent = overlay_parent
        self._placeholder = control
        # Never leave the XDL menulist / closed combo visible in the transcript.
        set_control_visible(control, False)
        self._popup_window: Any = None
        self._popup_floater: Any = None
        self.control: Any = None
        self._frame_handler: Any = None
        self._frame_controller: Any = None
        self._toolkit_handler: Any = None
        self._toolkit: Any = None
        self._ignore_item = False
        self._open = False
        self._matches: list[SlashCommand] = []
        self._selected = 0
        self._listeners_attached = False
        # Do not createWindow here. Overlay is born on first `/`.
        # Live XDL placeholder is a real ListBox with no Ask-peer test mock —
        # do not attach keys to it or the toolkit window never gets them.
        query_has_peer = callable(getattr(query_control, "getPeer", None))
        if control is not None and not _is_combo_box(control) and not query_has_peer:
            self.control = control
            self.hide()
            self._attach_click()
            self._attach_keys()
            self._listeners_attached = True

    def _bind_overlay(self) -> None:
        if self._popup_window is not None:
            return
        created = _create_ask_peer_listbox(self.query_control, self._overlay_parent)
        if created is None:
            return
        win, floater = created
        self._popup_window = win
        self._popup_floater = floater
        self.control = win
        self._listeners_attached = False
        self._attach_click()
        self._attach_keys()
        self._attach_frame_keys()
        self._attach_toolkit_keys()
        self._listeners_attached = True
        _ovlog("listeners attached to toolkit overlay")

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
        _ovlog("hide open_was=%s toolkit=%s", self._open, self._popup_window is not None)
        self._detach_frame_keys()
        self._detach_toolkit_keys()
        self._open = False
        self._matches = []
        self._selected = 0
        set_control_visible(self._placeholder, False)
        if self._popup_window is not None:
            win = self._popup_window
            set_control_visible(win, False)
            dispose = getattr(win, "dispose", None)
            if callable(dispose):
                try:
                    dispose()
                    _ovlog("hide disposed toolkit window")
                except Exception:
                    _ovlog("hide dispose failed", exc_info=True)
            floater = getattr(self, "_popup_floater", None)
            if floater is not None:
                dispose_f = getattr(floater, "dispose", None)
                if callable(dispose_f):
                    try:
                        dispose_f()
                        _ovlog("hide disposed floater")
                    except Exception:
                        _ovlog("hide dispose floater failed", exc_info=True)
            self._popup_floater = None
            self._popup_window = None
            self.control = None
            self._listeners_attached = False
            return
        set_control_visible(self.control, False)
        _ovdiag(self.control, "after hide")

    def on_query_text(self, text: str) -> None:
        """Open or narrow the popup from the current Ask-box contents."""
        prefix = slash_typed_prefix(text)
        _ovlog("on_query_text prefix=%r text=%r", prefix, (text[:40] if isinstance(text, str) else text))
        if prefix is None:
            _ovlog("on_query_text hide (not a slash prefix)")
            self.hide()
            return
        matches = filter_slash_commands(text, load_slash_lru(), SLASH_COMMANDS)
        _ovlog("on_query_text matches=%s names=%s", len(matches), [c.name for c in matches[:8]])
        if not matches:
            _ovlog("on_query_text hide (no matches)")
            self.hide()
            return
        try:
            self._show_matches(matches)
        except Exception:
            _ovlog("on_query_text show failed", exc_info=True)

    def _show_matches(self, matches: list[SlashCommand]) -> None:
        import threading
        _ovlog(
            "show_matches thread=%s bind_win=%s control=%s",
            threading.current_thread().name,
            self._popup_window is not None,
            self.control is not None,
        )
        self._bind_overlay()
        _ovlog("after bind control=%s toolkit=%s", self.control is not None, self._popup_window is not None)
        if self.control is None:
            _ovlog("on_query_text NO overlay list; slash prefix ignored")
            log.warning("slash popup: no overlay list; slash prefix ignored")
            return
        self._matches = matches
        self._selected = 0
        self._refresh_list()
        self._open = True
        self.reposition()
        set_control_visible(self._placeholder, False)
        _ovdiag(self.control, "before setVisible True")
        host = getattr(self, "_popup_floater", None)
        if host is not None:
            set_control_visible(host, True)
        set_control_visible(self.control, True)
        to_front = getattr(host, "toFront", None) if host is not None else getattr(self.control, "toFront", None)
        if callable(to_front):
            try:
                to_front()
                _ovlog("toFront called")
            except Exception:
                _ovlog("toFront failed", exc_info=True)
        else:
            _ovlog("toFront missing")
        _ovdiag(self.control, "after setVisible True")
        self._fill_visible_list()
        set_focus = getattr(self.query_control, "setFocus", None)
        if callable(set_focus):
            with suppress_disposed("slash popup return focus", logger=log):
                set_focus()
                _ovlog("Ask setFocus after show")

    def handle_key(self, key_code: int, modifiers: int = 0) -> bool:
        """True when the popup consumed the key (do not Send / insert newline)."""
        _ovlog("handle_key code=%s mods=%s open=%s", key_code, modifiers, self._open)
        if not self._open:
            return False
        action = classify_slash_key(key_code, modifiers)
        _ovlog("handle_key action=%s", action)
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
        self.hide()

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
            relative_to_ask = self._overlay_parent is None
            x, y, w, h = _overlay_rect(qr, rows, relative_to_ask=relative_to_ask)
            screen = _location_on_screen(query)
            if screen is not None and self._popup_window is not None:
                x, y = int(screen[0]), int(screen[1]) - h - 2
            _ovlog(
                "reposition Ask=%sx%s@%s,%s popup_bounds=%s,%s %sx%s rows=%s toolkit=%s relative_to_ask=%s",
                qr.Width,
                qr.Height,
                qr.X,
                qr.Y,
                x,
                y,
                w,
                h,
                rows,
                self._popup_window is not None,
                relative_to_ask,
            )
            if self._popup_window is not None:
                floater = getattr(self, "_popup_floater", None)
                if floater is not None:
                    floater.setPosSize(x, y, w, h, _POS_SIZE_FLAGS)
                    ctrl.setPosSize(0, 0, w, h, _POS_SIZE_FLAGS)
                    _ovdiag(floater, "after setPosSize floater")
                    _ovdiag(ctrl, "after setPosSize listbox")
                else:
                    ctrl.setPosSize(x, y, w, h, _POS_SIZE_FLAGS)
                    _ovdiag(ctrl, "after setPosSize toolkit")
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

    def _fill_visible_list(self) -> None:
        """Populate after the overlay is on screen. getItemCount before show blocks VCL."""
        ctrl = self.control
        if ctrl is None:
            return
        labels = tuple(format_slash_item(cmd) for cmd in self._matches)
        methods = [
            n
            for n in (
                "addItems",
                "getItemCount",
                "getItem",
                "getModel",
                "makeVisible",
                "invalidate",
                "selectItemPos",
                "setDropDownLineCount",
            )
            if callable(getattr(ctrl, n, None))
        ]
        _ovlog("fill_visible n=%s first=%r methods=%s", len(labels), labels[0] if labels else None, methods)
        add = getattr(ctrl, "addItems", None)
        if callable(add):
            try:
                add(labels, 0)
                _ovlog("fill_visible addItems after show ok")
            except Exception:
                _ovlog("fill_visible addItems failed", exc_info=True)
                return
        gc = getattr(ctrl, "getItemCount", None)
        if callable(gc):
            try:
                _ovlog("fill_visible count=%s", gc())
            except Exception:
                _ovlog("fill_visible getItemCount failed", exc_info=True)
                return
        gi = getattr(ctrl, "getItem", None)
        if callable(gi):
            try:
                _ovlog("fill_visible item0=%r", gi(0))
            except Exception:
                _ovlog("fill_visible getItem failed", exc_info=True)
        mv = getattr(ctrl, "makeVisible", None)
        if callable(mv):
            try:
                mv(0)
                _ovlog("fill_visible makeVisible 0 ok")
            except Exception:
                _ovlog("fill_visible makeVisible failed", exc_info=True)
        inv = getattr(ctrl, "invalidate", None)
        if callable(inv):
            try:
                inv(0)
                _ovlog("fill_visible invalidate ok")
            except Exception:
                _ovlog("fill_visible invalidate failed", exc_info=True)
        self._select_row(0)

    def _refresh_list(self) -> None:
        # Headed: toolkit VCL listbox hung inside getItemCount/addItems so
        # setVisible True never ran. Skip UNO fill; show the empty window.
        if self._popup_window is not None:
            _ovlog("refresh skip UNO fill on toolkit window")
            return
        ctrl = self.control
        if ctrl is None:
            _ovlog("refresh skip, no control")
            return
        labels = tuple(format_slash_item(cmd) for cmd in self._matches)
        _ovlog(
            "refresh n=%s has_getItemCount=%s has_addItems=%s has_selectItemPos=%s",
            len(labels),
            hasattr(ctrl, "getItemCount"),
            hasattr(ctrl, "addItems"),
            hasattr(ctrl, "selectItemPos"),
        )
        with suppress_disposed("slash popup refresh", logger=log):
            if hasattr(ctrl, "getItemCount") and hasattr(ctrl, "removeItems"):
                _ovlog("refresh getItemCount...")
                count = int(ctrl.getItemCount() or 0)
                _ovlog("refresh count=%s", count)
                if count:
                    ctrl.removeItems(0, count)
                    _ovlog("refresh removeItems done")
            if labels and hasattr(ctrl, "addItems"):
                _ovlog("refresh addItems first=%r", labels[0])
                ctrl.addItems(labels, 0)
                _ovlog("refresh addItems done")
            self._select_row(0)
            _ovlog("refresh select done")

    def _select_row(self, idx: int) -> None:
        ctrl = self.control
        if ctrl is None or not self._matches:
            return
        idx = max(0, min(idx, len(self._matches) - 1))
        self._selected = idx
        if hasattr(ctrl, "selectItemPos"):
            self._ignore_item = True
            try:
                with suppress_disposed("slash popup select", logger=log):
                    ctrl.selectItemPos(idx, True)
            finally:
                self._ignore_item = False

    def _attach_frame_keys(self) -> None:
        """Catch Esc/Enter even when the TOP overlay or document has focus."""
        if self._frame_handler is not None:
            return
        frame = getattr(self.send_listener, "frame", None)
        get_controller = getattr(frame, "getController", None) if frame is not None else None
        if not callable(get_controller):
            _ovlog("frame key handler: no controller")
            return
        try:
            import unohelper
            from com.sun.star.awt import XKeyHandler
        except ImportError:
            return
        try:
            controller = get_controller()
        except Exception:
            _ovlog("frame key handler: getController failed", exc_info=True)
            return
        add = getattr(controller, "addKeyHandler", None)
        if not callable(add):
            _ovlog("frame key handler: no addKeyHandler")
            return
        host = self

        class _Handler(unohelper.Base, XKeyHandler):  # type: ignore[misc]
            def disposing(self, Source):  # noqa: N802, N803 -- UNO signature
                return

            def keyPressed(self, aEvent):  # noqa: N802 -- UNO XKeyHandler
                code = int(getattr(aEvent, "KeyCode", 0) or 0)
                mods = int(getattr(aEvent, "Modifiers", 0) or 0)
                ch = getattr(aEvent, "KeyChar", None)
                _ovlog("frame keyPressed code=%s mods=%s char=%r", code, mods, ch)
                return bool(host.handle_key(code, mods))

            def keyReleased(self, aEvent):  # noqa: N802 -- UNO XKeyHandler
                return False

        handler = _Handler()
        try:
            add(handler)
            self._frame_handler = handler
            self._frame_controller = controller
            _ovlog("frame key handler attached")
        except Exception:
            _ovlog("frame key handler attach failed", exc_info=True)

    def _detach_frame_keys(self) -> None:
        handler = self._frame_handler
        controller = self._frame_controller
        self._frame_handler = None
        self._frame_controller = None
        if controller is None or handler is None:
            return
        rem = getattr(controller, "removeKeyHandler", None)
        if not callable(rem):
            return
        try:
            rem(handler)
            _ovlog("frame key handler removed")
        except Exception:
            _ovlog("frame key handler remove failed", exc_info=True)

    def _attach_toolkit_keys(self) -> None:
        """Catch Esc/Enter on the TOP floater, which does not route through Ask or the doc frame."""
        if self._toolkit_handler is not None:
            return
        peer = None
        for obj in (self._popup_floater, self.control, self.query_control, self._overlay_parent):
            if obj is None:
                continue
            peer = _query_peer(obj)
            if peer is not None:
                break
        tk = _toolkit_for_peer(peer)
        if tk is None:
            _ovlog("toolkit key handler: no toolkit")
            return
        add = getattr(tk, "addKeyHandler", None)
        names = [n for n in dir(tk) if "ey" in n.lower() or "andler" in n.lower() or "oolkit" in n.lower()]
        _ovlog("toolkit key handler methods=%s addKeyHandler=%s", names[:20], callable(add))
        if not callable(add):
            return
        try:
            import unohelper
            from com.sun.star.awt import XKeyHandler
        except ImportError:
            return
        host = self

        class _Handler(unohelper.Base, XKeyHandler):  # type: ignore[misc]
            def disposing(self, Source):  # noqa: N802, N803 -- UNO signature
                return

            def keyPressed(self, aEvent):  # noqa: N802 -- UNO XKeyHandler
                code = int(getattr(aEvent, "KeyCode", 0) or 0)
                mods = int(getattr(aEvent, "Modifiers", 0) or 0)
                ch = getattr(aEvent, "KeyChar", None)
                _ovlog("toolkit keyPressed code=%s mods=%s char=%r", code, mods, ch)
                return bool(host.handle_key(code, mods))

            def keyReleased(self, aEvent):  # noqa: N802 -- UNO XKeyHandler
                return False

        handler = _Handler()
        try:
            add(handler)
            self._toolkit_handler = handler
            self._toolkit = tk
            _ovlog("toolkit key handler attached")
        except Exception:
            _ovlog("toolkit key handler attach failed", exc_info=True)

    def _detach_toolkit_keys(self) -> None:
        handler = self._toolkit_handler
        tk = self._toolkit
        self._toolkit_handler = None
        self._toolkit = None
        if tk is None or handler is None:
            return
        rem = getattr(tk, "removeKeyHandler", None)
        if not callable(rem):
            return
        try:
            rem(handler)
            _ovlog("toolkit key handler removed")
        except Exception:
            _ovlog("toolkit key handler remove failed", exc_info=True)

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
                _ovlog("mousePressed y=%s", getattr(e, "Y", None))
                return

            def mouseReleased(self, e):  # noqa: N802 -- UNO signature
                y = int(getattr(e, "Y", -1) or -1)
                _ovlog("mouseReleased y=%s open=%s", y, host._open)
                host.accept_row_at_y(y)

            def mouseEntered(self, e):  # noqa: N802 -- UNO signature
                return

            def mouseExited(self, e):  # noqa: N802 -- UNO signature
                return

        try:
            ctrl.addMouseListener(_Click())
            _ovlog("mouse listener attached")
        except Exception:
            _ovlog("mouse listener attach failed", exc_info=True)
        self._attach_item_listener()

    def _attach_item_listener(self) -> None:
        ctrl = self.control
        if ctrl is None:
            return
        try:
            import unohelper
            from com.sun.star.awt import XItemListener, XActionListener
        except ImportError:
            return
        host = self

        class _Item(unohelper.Base, XItemListener):  # type: ignore[misc]
            def disposing(self, Source):  # noqa: N802, N803 -- UNO signature
                return

            def itemStateChanged(self, rEvent):  # noqa: N802 -- UNO XItemListener
                _ovlog("itemStateChanged ignore=%s open=%s", host._ignore_item, host._open)
                if host._ignore_item or not host._open:
                    return
                host.accept_selected()

        class _Act(unohelper.Base, XActionListener):  # type: ignore[misc]
            def disposing(self, Source):  # noqa: N802, N803 -- UNO signature
                return

            def actionPerformed(self, rEvent):  # noqa: N802 -- UNO XActionListener
                _ovlog("actionPerformed open=%s", host._open)
                if host._open:
                    host.accept_selected()

        if hasattr(ctrl, "addItemListener"):
            try:
                ctrl.addItemListener(_Item())
                _ovlog("item listener attached")
            except Exception:
                _ovlog("item listener attach failed", exc_info=True)
        if hasattr(ctrl, "addActionListener"):
            try:
                ctrl.addActionListener(_Act())
                _ovlog("action listener attached")
            except Exception:
                _ovlog("action listener attach failed", exc_info=True)

    def _attach_keys(self) -> None:
        """Forward Esc/Enter/arrows when the list stole focus from Ask."""
        try:
            import unohelper
            from com.sun.star.awt import XKeyListener
        except ImportError:
            _ovlog("key listener: no unohelper")
            return

        host = self

        class _Keys(unohelper.Base, XKeyListener):  # type: ignore[misc]
            def disposing(self, Source):  # noqa: N802, N803 -- UNO signature
                return

            def keyPressed(self, e):  # noqa: N802 -- UNO signature
                _ovlog(
                    "overlay/dialog keyPressed code=%s mods=%s",
                    int(getattr(e, "KeyCode", 0) or 0),
                    int(getattr(e, "Modifiers", 0) or 0),
                )
                if host.handle_key(int(getattr(e, "KeyCode", 0) or 0), int(getattr(e, "Modifiers", 0) or 0)):
                    with suppress_disposed("slash list Consume", logger=log):
                        if hasattr(e, "Consume"):
                            setattr(e, "Consume", True)
                return

            def keyReleased(self, e):  # noqa: N802 -- UNO signature
                return

        targets = (
            ("overlay", self.control),
            ("floater", getattr(self, "_popup_floater", None)),
            ("ask", self.query_control),
            ("dialog", self._overlay_parent),
        )
        listener = _Keys()
        for label, ctrl in targets:
            if ctrl is None or not hasattr(ctrl, "addKeyListener"):
                _ovlog("key listener skip %s", label)
                continue
            try:
                ctrl.addKeyListener(listener)
                _ovlog("key listener attached %s", label)
            except Exception:
                _ovlog("key listener attach failed %s", label, exc_info=True)
