# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Steerable slash-command completion menu attached to the sidebar Ask field.

Prototype: disabled (ENABLE_SLASH = False) and kept on purpose; it will be enabled one day. Do not suggest deleting it.

A native ``PopupMenu`` is modal — the user could not keep typing. The XDL
placeholder is ``dlg:menulist`` (LibreOffice dialog.dtd has no ``listbox``).

PR 564 parked an in-dialog ListBox in the Ready/Ask gap (siblings cannot
paint over the rich-text transcript). PR 575/584 tried TOP then SIMPLE for
Ask focus. Arch HiDPI: SIMPLE gap-fit still crushed under Ready/query_label.
Product placement: up to 6 rows **above Ready** over the transcript. The host
is a borderless ``floatingwindow``: with no WindowAttributes VCL makes it an
overlap window inside the document frame (no system window, no WM title bar,
paints over the sidebar). A ``"window"`` + WindowClass TOP host is a
``WorkWindow``; toolkit ignores NODECORATION for it, so KDE/qt6 showed a
decorated "soffice.bin" window. Do **not** resize or hide rich-text. Fill
before show, idle post from textChanged, and return focus to Ask after map.
"""

from __future__ import annotations

import logging
from typing import Any

from plugin.chatbot.dialogs import get_control_text, set_control_text, set_control_visible
from plugin.chatbot.slash_commands import (
    SlashCommand,
    classify_slash_key,
    filter_slash_commands,
    format_slash_item,
    load_slash_lru,
    registered_slash_commands,
    run_slash_command,
    slash_typed_prefix,
)
from plugin.framework.errors import best_effort, suppress_disposed, uno_probe
from plugin.framework.uno_listeners import BaseKeyListener, BaseMouseListener

log = logging.getLogger("writeragent.slash_popup")

# Slash-overlay tracing is very noisy. Set True to log these steps to the
# debug log even when log_level is DEBUG. Stays False in tree (same pattern
# as PANEL_RESIZE_VERBOSE_DEBUG / RICH_SCROLL_VERBOSE_DEBUG).
SLASH_OV_VERBOSE_DEBUG = False


def _ovlog(msg: str, *args: object, exc_info: bool = False) -> None:
    """Slash-overlay breadcrumb. Logger only — no /tmp (Bandit B108)."""
    if not SLASH_OV_VERBOSE_DEBUG:
        return
    text = (msg % args) if args else msg
    log.debug("[SLASH-OV] %s", text, exc_info=exc_info)


def _defer_to_next_turn(fn: Any, *args: Any) -> None:
    """Run ``fn`` on a later main-thread turn. Do not call it inline on failure.

    Accept and Esc must not reach ``hide()`` on the listener stack.
    ``accept_selected`` → ``run_slash_command`` → ``hide()`` (and Esc →
    ``hide()``) ``dispose()`` the toolkit window from inside its own
    ``XKeyHandler`` / ``XMouseListener`` while VCL is still in that callback.
    ``post_to_main_thread`` enqueues once AsyncCallback exists (the headed
    sidebar). The listener returns before dispose. An inline fallback is
    the same deadlock, so a failed post is only logged.
    """
    try:
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(fn, *args)
    except Exception:
        log.warning("slash popup: could not defer main-thread turn", exc_info=True)


def _ovdiag(obj: Any, label: str) -> None:
    """Peer/window geometry + visibility for headed overlay debugging."""
    # Probes getPosSize / peers. Skip them unless the verbose flag is on;
    # _ovlog would drop the line anyway.
    if not SLASH_OV_VERBOSE_DEBUG:
        return
    if obj is None:
        _ovlog("%s None", label)
        return
    bits = [label, "py=%s" % type(obj).__name__]
    try:
        bits.append("impl=%s" % obj.getImplementationName())
        ps = obj.getPosSize()
        bits.append("pos=%sx%s@%s,%s" % (ps.Width, ps.Height, ps.X, ps.Y))
        for name in ("isVisible", "isReallyVisible"):
            fn = getattr(obj, name, None)
            if callable(fn):
                bits.append("%s=%s" % (name, fn()))
        bits.append("toFront=%s" % callable(getattr(obj, "toFront", None)))
        bits.append("setVisible=%s" % callable(getattr(obj, "setVisible", None)))
        bits.append("output=%sx%s" % (obj.getOutputWidth(), obj.getOutputHeight()))
        osz = obj.getOutputSize()
        bits.append("outputSize=%sx%s" % (osz.Width, osz.Height))
        peer = obj.getPeer() if callable(getattr(obj, "getPeer", None)) else None
        if peer is not None and peer is not obj:
            pps = peer.getPosSize()
            bits.append("peerPos=%sx%s@%s,%s" % (pps.Width, pps.Height, pps.X, pps.Y))
            bits.append("peerOutput=%sx%s" % (peer.getOutputWidth(), peer.getOutputHeight()))
            posz = peer.getOutputSize()
            bits.append("peerOutputSize=%sx%s" % (posz.Width, posz.Height))
    except Exception:
        log.debug("slash overlay geometry probe failed for %s", label, exc_info=True)
    _ovlog("%s", " ".join(str(b) for b in bits))


def _resolve_query_label(send_listener: Any, overlay_parent: Any = None) -> Any:
    """Ask/instruct label control — may sit in the Ready↔Ask gap on HiDPI."""
    label = getattr(send_listener, "query_label", None) if send_listener is not None else None
    if label is not None:
        return label
    parent = overlay_parent
    if parent is None and send_listener is not None:
        popup = getattr(send_listener, "slash_popup", None)
        parent = getattr(popup, "_overlay_parent", None) if popup is not None else None
    if parent is not None and hasattr(parent, "getControl"):
        return parent.getControl("query_label")
    return None


def _log_slash_clip_neighbors(
    send_listener: Any,
    *,
    popup_bounds: tuple[int, int, int, int] | None = None,
    overlay_parent: Any = None,
) -> None:
    """Log status / query_label / response / rich-text geometry beside popup (Arch clip)."""
    if popup_bounds is not None:
        x, y, w, h = popup_bounds
        _ovlog("popup_bounds_for_clip=%s,%s %sx%s (y..y+h=%s..%s)", x, y, w, h, y, y + h)
    if send_listener is None:
        _ovlog("clip_neighbors send_listener None")
        return
    status = getattr(send_listener, "status_control", None)
    response = getattr(send_listener, "response_control", None)
    query_label = _resolve_query_label(send_listener, overlay_parent)
    rich_widget = getattr(send_listener, "rich_text_widget", None)
    rich_ctrl = getattr(rich_widget, "control", None) if rich_widget is not None else None
    _ovdiag(status, "status")
    _ovdiag(query_label, "query_label")
    _ovdiag(response, "response")
    _ovdiag(rich_ctrl, "rich_text")

# Feature flag: slash-command popup is currently disabled until stable.
ENABLE_SLASH = False

_POPUP_MAX_ROWS = 6
_POPUP_ROW_PX = 14
_POS_SIZE_FLAGS = 15  # X + Y + WIDTH + HEIGHT


def _supported_services(ctrl: Any) -> str:
    model = ctrl.getModel() if ctrl is not None else None
    if model is not None and hasattr(model, "getSupportedServiceNames"):
        return " ".join(str(s) for s in model.getSupportedServiceNames())
    return ""


def _is_combo_box(ctrl: Any) -> bool:
    """True for the XDL ``menulist`` placeholder (ComboBox), not a ListBox."""
    names = _supported_services(ctrl)
    return "ComboBox" in names or "UnoControlComboBox" in names


def _overlay_height(rows: int) -> int:
    return max(24, min(max(rows, 1), _POPUP_MAX_ROWS) * _POPUP_ROW_PX + 6)


def _list_height(ctrl: Any, rows: int) -> int:
    """Height for ``rows`` lines in the list's own font, border included.

    ListBox ``getMinimumSize(0, n)`` (XTextLayoutConstrains) is VCL's
    CalcBlockSize, so qt6/kf6 and HiDPI rows (18-40px) are not clipped to
    the 14px estimate. Mocks without it keep ``_overlay_height``.
    """
    n = min(max(rows, 1), _POPUP_MAX_ROWS)
    fn = getattr(ctrl, "getMinimumSize", None)
    size = uno_probe(lambda: fn(0, n), None) if callable(fn) else None
    height = getattr(size, "Height", None)
    if isinstance(height, int) and height > 0:
        return height
    return _overlay_height(n)


def _popup_bounds(query_width: int, rows: int) -> tuple[int, int, int, int]:
    """Ask-peer-relative rectangle: tall list just above the field (Y negative)."""
    height = _overlay_height(rows)
    return (0, -height - 2, max(20, int(query_width)), height)


def _printable_key_char(key_char: Any) -> str | None:
    if key_char is None:
        return None
    raw: Any = key_char
    if not isinstance(raw, str):
        raw = getattr(key_char, "value", None)
    if not isinstance(raw, str) or len(raw) != 1:
        return None
    if not raw.isprintable() or raw in "\t\n\r":
        return None
    return raw


def _overlay_rect(qr: Any, rows: int, *, relative_to_ask: bool) -> tuple[int, int, int, int]:
    """List rectangle. Ask-relative Y is negative (clips in a 49px peer). Dialog-relative sits above Ask."""
    width = max(20, int(getattr(qr, "Width", 0) or 0))
    if relative_to_ask:
        return _popup_bounds(width, rows)
    height = _overlay_height(rows)
    return (int(qr.X), int(qr.Y) - height - 2, width, height)


def _status_control(send_listener: Any) -> Any:
    return getattr(send_listener, "status_control", None) if send_listener is not None else None


def _status_top(send_listener: Any) -> int | None:
    """Top Y of the Ready/status control (dialog-local), or None."""
    status = _status_control(send_listener)
    if status is None or not hasattr(status, "getPosSize"):
        return None
    return int(status.getPosSize().Y)


def _location_on_screen(obj: Any) -> tuple[int, int] | None:
    """Screen origin of an AWT window/control, or None."""
    if obj is None:
        return None
    get_acc = getattr(obj, "getAccessibleContext", None)
    if not callable(get_acc):
        return None
    acc = uno_probe(get_acc, None)
    fn = getattr(acc, "getLocationOnScreen", None) if acc is not None else None
    if not callable(fn):
        return None
    pt = uno_probe(fn, None)
    if pt is None:
        return None
    x = getattr(pt, "X", None)
    y = getattr(pt, "Y", None)
    if x is None or y is None:
        return None
    return int(x), int(y)


def _overlay_offset(host: Any, ask_peer: Any, qr: Any) -> tuple[int, int] | None:
    """Add to dialog-local coords to get the host's ``setPosSize`` coords.

    The floatingwindow host is an overlap window of the document frame, so
    its position is relative to that frame, not the sidebar dialog and not
    the screen. ``host`` must still sit at (0, 0). Both points come from
    VCL's accessible screen location, so the difference holds even where
    absolute screen coordinates are not real (Wayland).
    """
    origin = _location_on_screen(host)
    ask = _location_on_screen(ask_peer)
    if origin is None or ask is None:
        return None
    return (
        ask[0] - origin[0] - int(getattr(qr, "X", 0) or 0),
        ask[1] - origin[1] - int(getattr(qr, "Y", 0) or 0),
    )


def _above_ready_rect(
    qr: Any,
    rows: int,
    *,
    status_top: int | None,
    height: int | None = None,
) -> tuple[int, int, int, int]:
    """Dialog-local rect: ~6 rows above Ready (over transcript), else classic above Ask."""
    width = max(20, int(getattr(qr, "Width", 0) or 0))
    h = height if height is not None else _overlay_height(rows)
    x = int(getattr(qr, "X", 0) or 0)
    if status_top is not None:
        y = int(status_top) - h - 2
        _ovlog("above_ready status_top=%s y=%s h=%s rows=%s", status_top, y, h, rows)
        return x, y, width, h
    return x, int(getattr(qr, "Y", 0) or 0) - h - 2, width, h


def _row_index_at_y(y: int, n_rows: int, row_px: int = _POPUP_ROW_PX) -> int | None:
    """List row under a mouse Y, or None when the click is not on a row."""
    if y < 0 or n_rows <= 0:
        return None
    row = int(y) // max(1, int(row_px))
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
    peer = uno_probe(get_peer, None)
    if peer is None:
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
        return uno_probe(peer.getToolkit, None)
    return None


def _awt_window_constants() -> tuple[Any, Any, int, int, int] | None:
    """WindowClass / WindowAttribute as IDL integers (enum modules are untyped)."""
    try:
        from com.sun.star.awt import Rectangle, WindowDescriptor
    except ImportError:
        return None
    # com.sun.star.awt.WindowClass: TOP=0, SIMPLE=3
    # com.sun.star.awt.WindowAttribute: SHOW=1, BORDER=16
    # SHOW at create maps the host before addItems; idle addItems then deadlocks.
    return Rectangle, WindowDescriptor, 0, 3, 16


def _create_ask_peer_listbox(
    query_control: Any,
    parent_control: Any = None,
    send_listener: Any = None,
) -> Any:
    """Borderless floatingwindow host + SIMPLE listbox child above Ready.

    ``floatingwindow`` with WindowAttributes 0 is a VCL overlap window in
    the document frame: no system window, so no WM title bar or taskbar
    entry on any backend, and it paints over the rich-text transcript.
    History stays full size. Never set SHOW at create (PR 584 deadlock).
    Fill before show, then setVisible, then return focus to Ask.
    Returns ``(listbox, host, offset)``; ``offset`` maps dialog-local
    rectangles to host coordinates (see ``_overlay_offset``).
    """
    ask_peer = _query_peer(query_control)
    parent = ask_peer
    if parent_control is not None:
        overlay_peer = _query_peer(parent_control, label="dialog")
        if overlay_peer is not None:
            parent = overlay_peer
            _ovdiag(overlay_peer, "dialog/overlay parent peer")
    if parent is None:
        return None
    toolkit = _toolkit_for_peer(parent)
    if toolkit is None or not hasattr(toolkit, "createWindow"):
        _ovlog("no toolkit on overlay parent")
        return None
    consts = _awt_window_constants()
    if consts is None:
        return None
    rectangle_cls, descriptor_cls, top, simple, child_attrs = consts
    qr = query_control.getPosSize() if hasattr(query_control, "getPosSize") else None
    if qr is None:
        qr = type("R", (), {"X": 0, "Y": 0, "Width": 142, "Height": 30})()
    _x, _y, w, h = _above_ready_rect(qr, _POPUP_MAX_ROWS, status_top=_status_top(send_listener))
    host_desc = descriptor_cls()
    host_desc.Type = top  # floatingwindow ignores the class; parent decides the frame
    host_desc.WindowServiceName = "floatingwindow"
    host_desc.Parent = parent
    host_desc.ParentIndex = -1
    # (0, 0) so _overlay_offset can read the frame origin off the host.
    host_desc.Bounds = rectangle_cls(0, 0, w, h)
    host_desc.WindowAttributes = 0  # any border/move/close bit makes it a system window
    _ovlog("createWindow floatingwindow %sx%s", w, h)
    try:
        host = toolkit.createWindow(host_desc)
    except Exception:
        _ovlog("createWindow floatingwindow FAILED", exc_info=True)
        host = None
    if host is None:
        log.warning("slash popup: overlay window could not be created")
        return None
    _ovdiag(host, "overlay-host")
    desc = descriptor_cls()
    desc.Type = simple
    desc.WindowServiceName = "listbox"
    desc.Parent = host
    desc.ParentIndex = -1
    desc.Bounds = rectangle_cls(0, 0, w, h)
    desc.WindowAttributes = child_attrs
    try:
        win = toolkit.createWindow(desc)
    except Exception:
        _ovlog("createWindow listbox FAILED", exc_info=True)
        win = None
    offset = _overlay_offset(host, ask_peer, qr) if win is not None else None
    if win is None or offset is None:
        log.warning("slash popup: overlay list or its position unavailable")
        for obj in (win, host):
            if obj is not None and hasattr(obj, "dispose"):
                best_effort(obj.dispose, "slash popup: overlay dispose failed")
        return None
    _ovlog("overlay offset dialog->host %s", offset)
    return win, host, offset


def _make_slash_key_handler(host: Any, label: str) -> Any:
    try:
        import unohelper
        from com.sun.star.awt import XKeyHandler
    except ImportError:
        return None

    class _Handler(unohelper.Base, XKeyHandler):  # type: ignore[misc]
        def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
            return

        def keyPressed(self, aEvent: Any) -> bool:  # noqa: N802 -- UNO XKeyHandler
            code = int(getattr(aEvent, "KeyCode", 0) or 0)
            mods = int(getattr(aEvent, "Modifiers", 0) or 0)
            ch = getattr(aEvent, "KeyChar", None)
            _ovlog("%s keyPressed code=%s mods=%s char=%r", label, code, mods, ch)
            return host._on_document_key(code, mods, ch)

        def keyReleased(self, aEvent: Any) -> bool:  # noqa: N802 -- UNO XKeyHandler
            return False

    return _Handler()


def _detach_key_handler(target: Any, handler: Any, label: str) -> None:
    if target is None or handler is None:
        return
    rem = getattr(target, "removeKeyHandler", None)
    if callable(rem):
        best_effort(lambda: rem(handler), f"{label} key handler remove failed")
        _ovlog(f"{label} key handler removed")


class SlashPopupController:
    """Show, filter, and accept slash commands on the Ask-field overlay list."""

    query_control: Any
    send_listener: Any
    _overlay_parent: Any
    _placeholder: Any
    _ignore_item: bool
    _open: bool
    _selected: int
    _listeners_attached: bool

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
        # Dialog-local -> overlay host offset from _overlay_offset.
        self._ask_origin: tuple[int, int] | None = None
        self._row_px = _POPUP_ROW_PX
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
            # Construction is not a dismiss. Do not emit TEXT_UPDATED here.
            self.hide(restore_send=False)
            self._attach_click()
            self._attach_keys()
            self._listeners_attached = True
        elif query_has_peer and ENABLE_SLASH:
            # The sidebar FocusManager consumes Esc in panel content (focus
            # moves to the panel title). It is an Application key listener
            # too, so ours must be registered first, at panel creation, and
            # stay registered: re-adding it on each show put it behind.
            self._attach_toolkit_keys()

    def dispose(self) -> None:
        """Panel teardown: close the popup and drop the toolkit key handler."""
        self.hide(restore_send=False)
        self._detach_toolkit_keys()

    def _bind_overlay(self) -> None:
        if self._popup_window is not None:
            return
        created = _create_ask_peer_listbox(self.query_control, self._overlay_parent, send_listener=self.send_listener)
        if created is None:
            return
        win, floater, origin = created
        self._popup_window = win
        self._popup_floater = floater
        self.control = win
        self._ask_origin = origin
        self._listeners_attached = False
        _ovlog("bound hidden origin=%s; click after fill", origin)

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

    def hide(self, *, restore_send: bool = True) -> None:
        """Close the popup. Callers inside a key/mouse callback must defer this.

        ``restore_send`` dispatches ``TEXT_UPDATED`` from the live Ask text
        after the toolkit window is disposed. Recreate passes False so a
        slash draft does not run UpdateUI while the next overlay is mapped.
        """
        _ovlog("hide open_was=%s toolkit=%s", self._open, self._popup_window is not None)
        self._detach_frame_keys()
        self._open = False
        self._matches = []
        self._selected = 0
        set_control_visible(self._placeholder, False)
        if self._popup_window is not None:
            win = self._popup_window
            floater = self._popup_floater
            set_control_visible(win, False)
            if floater is not None and floater is not win:
                set_control_visible(floater, False)
            self._dispose_overlay_windows(win, floater)
            self._popup_floater = None
            self._popup_window = None
            self.control = None
            self._ask_origin = None
            self._listeners_attached = False
            if restore_send:
                self._dispatch_query_has_text()
            return
        set_control_visible(self.control, False)
        _ovdiag(self.control, "after hide")
        if restore_send:
            self._dispatch_query_has_text()

    def _dispose_overlay_windows(self, win: Any, floater: Any) -> None:
        """Dispose the TOP host after the key/mouse callback has returned.

        ``hide()`` itself is deferred from Accept and Esc. Disposing here is
        then a later main-thread turn, not the listener stack.
        """
        dispose = getattr(win, "dispose", None)
        if callable(dispose):
            best_effort(dispose, "slash popup: hide dispose failed")
            _ovlog("hide disposed toolkit window")
        if floater is not None and floater is not win:
            dispose_f = getattr(floater, "dispose", None)
            if callable(dispose_f):
                best_effort(dispose_f, "slash popup: hide dispose floater failed")
                _ovlog("hide disposed floater")

    def _dispatch_query_has_text(self) -> None:
        """Re-enable Send from the text still in Ask.

        ``QueryTextListener`` returns before ``TEXT_UPDATED`` for any
        slash draft, and ``hide()`` / Esc never dispatched ``has_text``.
        After Esc, Ask still holds e.g. ``/he`` while Send stays disabled.
        The listener skips UpdateUI so a mapped TOP overlay does not
        ``getPosSize`` Ask (that deadlocks VCL). Dispatch only after the
        popup is closed, using Ask as the source of truth.
        """
        dispatch = getattr(self.send_listener, "dispatch", None)
        if not callable(dispatch):
            return
        from plugin.chatbot.send_state import SendEvent, SendEventKind

        text = get_control_text(self.query_control) or ""
        dispatch(SendEvent(SendEventKind.TEXT_UPDATED, has_text=bool(str(text).strip())))

    def on_query_text(self, text: str) -> None:
        """Open or narrow the popup from the current Ask-box contents."""
        if not ENABLE_SLASH:
            _ovlog("on_query_text disabled (ENABLE_SLASH=False)")
            return
        prefix = slash_typed_prefix(text)
        _ovlog("on_query_text prefix=%r text=%r", prefix, (text[:40] if isinstance(text, str) else text))
        if prefix is None:
            _ovlog("on_query_text hide (not a slash prefix)")
            # Bugfix: post_to_main_thread raises TimeoutError when the pending
            # list is full before AsyncCallback exists (queue_executor.post).
            # hide() / _show_matches() on this Ask textChanged stack deadlock
            # VCL. _defer_to_next_turn logs the failure and does not run them.
            _defer_to_next_turn(self.hide)
            return
        matches = filter_slash_commands(text, load_slash_lru(), registered_slash_commands())
        _ovlog("on_query_text matches=%s names=%s", len(matches), [c.name for c in matches[:8]])
        if not matches:
            _ovlog("on_query_text hide (no matches)")
            _defer_to_next_turn(self.hide)
            return
        # Never createWindow/setVisible inside Ask textChanged — returning from that
        # callback after a mapped TOP overlay deadlocks VCL and eats later keystrokes.
        new_names = [c.name for c in matches]
        if self._open and new_names == [c.name for c in self._matches]:
            _ovlog("on_query_text same matches skip recreate")
            return
        _defer_to_next_turn(self._show_matches, matches)
        _ovlog("on_query_text posted show n=%s", len(matches))

    def _show_matches(self, matches: list[SlashCommand]) -> None:
        import threading
        _ovlog(
            "show_matches thread=%s bind_win=%s control=%s",
            threading.current_thread().name,
            self._popup_window is not None,
            self.control is not None,
        )
        if self._popup_window is not None:
            # addItems/setPosSize on a mapped TOP window deadlocks; recreate via fill-before-show.
            # Not a dismiss: do not TEXT_UPDATED while the next overlay is about to map.
            _ovlog("show_matches recreate")
            self.hide(restore_send=False)
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
        self._fill_visible_list()
        _ovlog("filled before show")
        host = self._popup_floater
        if host is not None and host is not self.control:
            set_control_visible(host, True)
        set_control_visible(self.control, True)
        # Do not reposition() after setVisible. reposition getPosSize's Ask, and
        # once this TOP overlay is mapped that deadlocks VCL (panel.py
        # QueryTextListener). The pre-show reposition above is the one that counts.
        self._bring_overlay_front()
        self._late_attach_overlay_keys()
        self._attach_frame_keys()
        self._attach_toolkit_keys()
        # TOP host steals caret unless we return it — keep typing in Ask.
        set_focus = getattr(self.query_control, "setFocus", None)
        if callable(set_focus):
            with suppress_disposed("slash popup return focus", logger=log):
                set_focus()
                _ovlog("Ask setFocus after TOP show")
            # Idle re-focus: toFront/map can race past the first setFocus.
            try:
                from plugin.framework.queue_executor import post_to_main_thread

                def _refocus_ask() -> None:
                    sf = getattr(self.query_control, "setFocus", None)
                    if callable(sf):
                        with suppress_disposed("slash popup idle refocus", logger=log):
                            sf()
                            _ovlog("Ask setFocus idle refocus")

                post_to_main_thread(_refocus_ask)
            except Exception:
                _ovlog("Ask idle refocus post failed", exc_info=True)
        _ovlog("show complete TOP-above-Ready floater=%s", host is not None)

    def _late_attach_overlay_keys(self) -> None:
        """Mouse + overlay keys after SIMPLE show. Ask QueryKeyListener still gets Esc."""
        if self._listeners_attached or self.control is None:
            return
        self._attach_click()
        self._attach_keys()
        self._listeners_attached = True
        _ovlog("late mouse+overlay keys attached")

    def _on_document_key(self, key_code: int, modifiers: int = 0, key_char: Any = None) -> bool:
        """Frame and toolkit handlers: navigation only.

        The listbox listener is the one that has focus on the menu and
        must insert. Frame and toolkit handlers see the whole document:
        ``from_overlay=True`` appends every printable character to Ask and
        returns true, so document keystrokes never reach the document.
        ``from_overlay=False`` keeps printable keys for the document (or
        Ask's own listener) and only consumes Esc/arrows/Enter/Tab.
        """
        return bool(self.handle_key(key_code, modifiers, key_char, from_overlay=False))

    def _on_list_key(self, key_code: int, modifiers: int = 0, key_char: Any = None) -> bool:
        """Listbox listener: the menu has focus, so printable keys feed Ask."""
        return bool(self.handle_key(key_code, modifiers, key_char, from_overlay=True))

    def handle_key(
        self,
        key_code: int,
        modifiers: int = 0,
        key_char: Any = None,
        *,
        from_overlay: bool = False,
    ) -> bool:
        """True when the popup consumed the key (do not Send / insert newline)."""
        _ovlog("handle_key code=%s mods=%s open=%s overlay=%s", key_code, modifiers, self._open, from_overlay)
        if not self._open:
            return False
        action = classify_slash_key(key_code, modifiers)
        _ovlog("handle_key action=%s", action)
        if action is None:
            ch = _printable_key_char(key_char) if from_overlay else None
            if ch:
                cur = get_control_text(self.query_control) or ""
                nxt = cur + ch
                set_control_text(self.query_control, nxt)
                self.on_query_text(nxt)
                return True
            return False
        if action == "escape":
            # hide() disposes the toolkit window. Post it so this key callback returns first.
            _defer_to_next_turn(self.hide)
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
        name = self.selected_name
        if not name:
            _ovlog("accept_selected no name selected=%s matches=%s", self._selected, [c.name for c in self._matches])
            return
        _ovlog("accept_selected name=%s", name)
        # run_slash_command → hide() disposes the list. Post so the key or
        # mouse callback returns before dispose. hide() is not called again
        # here; a second hide would TEXT_UPDATED while the window still exists.
        host = self.send_listener
        _defer_to_next_turn(run_slash_command, name, host)

    def accept_row_at_y(self, y: int) -> bool:
        """Accept the command under mouse Y. Ignores chrome / off-list clicks."""
        row = _row_index_at_y(y, len(self._matches), self._row_px)
        if row is None or not self._open:
            return False
        self._selected = row
        self._select_row(row)
        self.accept_selected()
        return True


    def _bring_overlay_front(self) -> None:
        """Raise the toolkit listbox above Ready/transcript siblings (clip probe)."""
        for label, obj in (
            ("popup_window", self._popup_window),
            ("popup_floater", self._popup_floater),
            ("control", self.control),
        ):
            if obj is None:
                continue
            to_front = getattr(obj, "toFront", None)
            if not callable(to_front):
                _ovlog("toFront skip %s (no method)", label)
                continue
            best_effort(to_front, f"toFront failed {label}")
            _ovlog("toFront ok %s", label)

    def reposition(self) -> None:
        """Size the overlay to the match rows and park it above Ready (or Ask)."""
        query = self.query_control
        ctrl = self.control
        if query is None or ctrl is None or not hasattr(query, "getPosSize"):
            return
        with suppress_disposed("slash popup reposition", logger=log):
            qr = query.getPosSize()
            rows = min(len(self._matches) or 1, _POPUP_MAX_ROWS)
            relative_to_ask = self._overlay_parent is None
            status_top = _status_top(self.send_listener)
            if self._popup_window is not None:
                height = _list_height(ctrl, rows)
                one = _list_height(ctrl, 1)
                self._row_px = max(1, (height - one) // (rows - 1)) if rows > 1 else one
                x, y, w, h = _above_ready_rect(qr, rows, status_top=status_top, height=height)
            elif relative_to_ask:
                x, y, w, h = _overlay_rect(qr, rows, relative_to_ask=True)
            else:
                x, y, w, h = _above_ready_rect(qr, rows, status_top=status_top)
            _ovlog(
                "reposition Ask=%sx%s@%s,%s popup_bounds=%s,%s %sx%s rows=%s toolkit=%s relative_to_ask=%s origin=%s",
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
                self._ask_origin,
            )
            _log_slash_clip_neighbors(
                self.send_listener,
                popup_bounds=(x, y, w, h),
                overlay_parent=self._overlay_parent,
            )
            if self._popup_window is not None:
                floater = self._popup_floater
                ox, oy = self._ask_origin or (0, 0)
                if floater is not None and floater is not ctrl:
                    # Dialog-local -> frame overlay coords (host is not in the dialog).
                    floater.setPosSize(x + ox, y + oy, w, h, _POS_SIZE_FLAGS)
                    ctrl.setPosSize(0, 0, w, h, _POS_SIZE_FLAGS)
                else:
                    # Dialog-local SIMPLE (or single-window floater).
                    ctrl.setPosSize(x, y, w, h, _POS_SIZE_FLAGS)
                return
            # Mock list in unit tests: tall rectangle above Ask, never 14px closed.
            above_y = int(qr.Y) + y
            if above_y < 16:
                above_y = 16
            ctrl.setPosSize(int(qr.X), above_y, w, h, _POS_SIZE_FLAGS)

    def _fill_visible_list(self) -> None:
        """Fill the toolkit list before show.

        Non-toolkit lists are filled in ``_refresh_list``. ``addItems`` here
        too duplicated every row; that path has no VCL ``getItemCount`` deadlock.
        """
        if self._popup_window is None:
            return
        ctrl = self.control
        if ctrl is None:
            return
        labels = tuple(format_slash_item(cmd) for cmd in self._matches)
        _ovlog("fill_visible n=%s first=%r addItems-only", len(labels), labels[0] if labels else None)
        add = getattr(ctrl, "addItems", None)
        if callable(add):
            try:
                add(labels, 0)
                _ovlog("fill_visible addItems after show ok")
            except Exception:
                log.warning("slash popup: fill addItems failed", exc_info=True)

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
        controller = uno_probe(get_controller, None)
        if controller is None:
            return
        add = getattr(controller, "addKeyHandler", None)
        if not callable(add):
            _ovlog("frame key handler: no addKeyHandler")
            return
        handler = _make_slash_key_handler(self, "frame")
        if handler is None:
            return
        try:
            add(handler)
            self._frame_handler = handler
            self._frame_controller = controller
            _ovlog("frame key handler attached")
        except Exception:
            log.warning("slash popup: frame key handler attach failed", exc_info=True)

    def _detach_key_target(self, handler_name: str, target_name: str, label: str) -> None:
        handler = getattr(self, handler_name)
        target = getattr(self, target_name)
        setattr(self, handler_name, None)
        setattr(self, target_name, None)
        _detach_key_handler(target, handler, label)

    def _detach_frame_keys(self) -> None:
        self._detach_key_target("_frame_handler", "_frame_controller", "frame")

    def _attach_toolkit_keys(self) -> None:
        """Esc/Enter/arrows for the open popup, ahead of the sidebar FocusManager (see __init__)."""
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
        handler = _make_slash_key_handler(self, "toolkit")
        if handler is None:
            return
        try:
            add(handler)
            self._toolkit_handler = handler
            self._toolkit = tk
            _ovlog("toolkit key handler attached")
        except Exception:
            log.warning("slash popup: toolkit key handler attach failed", exc_info=True)

    def _detach_toolkit_keys(self) -> None:
        self._detach_key_target("_toolkit_handler", "_toolkit", "toolkit")

    def _attach_click(self) -> None:
        ctrl = self.control
        if ctrl is None or not hasattr(ctrl, "addMouseListener"):
            return

        host = self

        class _Click(BaseMouseListener):
            def mousePressed(self, e: Any) -> None:  # noqa: N802 -- UNO signature
                _ovlog("mousePressed y=%s", getattr(e, "Y", None))

            def mouseReleased(self, e: Any) -> None:  # noqa: N802 -- UNO signature
                y = int(getattr(e, "Y", -1) or -1)
                _ovlog("mouseReleased y=%s open=%s", y, host._open)
                host.accept_row_at_y(y)

        try:
            ctrl.addMouseListener(_Click())
            _ovlog("mouse listener attached")
        except Exception:
            log.warning("slash popup: mouse listener attach failed", exc_info=True)
        # skip item/action: addItemListener deadlocks idle addItems/show

    def _attach_keys(self) -> None:
        """Forward Esc/Enter/arrows when the list stole focus from Ask."""
        host = self

        class _Keys(BaseKeyListener):
            def keyPressed(self, e: Any) -> None:  # noqa: N802 -- UNO signature
                _ovlog(
                    "overlay/dialog keyPressed code=%s mods=%s",
                    int(getattr(e, "KeyCode", 0) or 0),
                    int(getattr(e, "Modifiers", 0) or 0),
                )
                if host._on_list_key(
                    int(getattr(e, "KeyCode", 0) or 0),
                    int(getattr(e, "Modifiers", 0) or 0),
                    getattr(e, "KeyChar", None),
                ):
                    with suppress_disposed("slash list Consume", logger=log):
                        if hasattr(e, "Consume"):
                            setattr(e, "Consume", True)

        targets = (("overlay", self.control),)
        listener = _Keys()
        for label, ctrl in targets:
            if ctrl is None or not hasattr(ctrl, "addKeyListener"):
                _ovlog("key listener skip %s", label)
                continue
            try:
                ctrl.addKeyListener(listener)
                _ovlog("key listener attached %s", label)
            except Exception:
                log.warning("slash popup: key listener attach failed %s", label, exc_info=True)
