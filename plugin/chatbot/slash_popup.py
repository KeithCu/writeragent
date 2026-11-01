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
    return "ComboBox" in _supported_services(ctrl)


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


def _status_top(send_listener: Any) -> int | None:
    """Top Y of the Ready/status control (dialog-local), or None."""
    status = getattr(send_listener, "status_control", None) if send_listener is not None else None
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


def _host_origin(host: Any, ctx: Any) -> tuple[int, int] | None:
    """Screen origin of the overlay host.

    LibreOffice 26.2 dropped XAccessible from toolkit peers, so the host
    itself has no accessible location there. A UnoControl still has one:
    put a 1px fixed text at the host's (0, 0), read it, dispose it.
    """
    origin = _location_on_screen(host)
    if origin is not None or ctx is None:
        return origin
    smgr = ctx.getServiceManager()
    probe = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlFixedText", ctx)
    probe.setModel(smgr.createInstanceWithContext("com.sun.star.awt.UnoControlFixedTextModel", ctx))
    try:
        probe.createPeer(host.getToolkit(), host)
        probe.setPosSize(0, 0, 1, 1, _POS_SIZE_FLAGS)
        return _location_on_screen(probe)
    finally:
        probe.dispose()


def _overlay_offset(host: Any, ask: Any, qr: Any, ctx: Any = None) -> tuple[int, int] | None:
    """Add to dialog-local coords to get the host's ``setPosSize`` coords.

    The floatingwindow host is an overlap window of the document frame, so
    its position is relative to that frame, not the sidebar dialog and not
    the screen. ``host`` must still sit at (0, 0). Both points come from
    VCL's accessible screen location, so the difference holds even where
    absolute screen coordinates are not real (Wayland).
    """
    origin = uno_probe(lambda: _host_origin(host, ctx), None)
    ask_xy = _location_on_screen(ask)
    if origin is None or ask_xy is None:
        return None
    return (
        ask_xy[0] - origin[0] - int(qr.X),
        ask_xy[1] - origin[1] - int(qr.Y),
    )


def _above_ready_rect(
    qr: Any,
    rows: int,
    *,
    status_top: int | None,
    height: int | None = None,
) -> tuple[int, int, int, int]:
    """Dialog-local rect: ~6 rows above Ready (over transcript), else classic above Ask."""
    width = max(20, int(qr.Width))
    h = height if height is not None else _overlay_height(rows)
    x = int(qr.X)
    if status_top is not None:
        y = int(status_top) - h - 2
        _ovlog("above_ready status_top=%s y=%s h=%s rows=%s", status_top, y, h, rows)
        return x, y, width, h
    return x, int(qr.Y) - h - 2, width, h


def _row_index_at_y(y: int, n_rows: int, row_px: int = _POPUP_ROW_PX) -> int | None:
    """List row under a mouse Y, or None when the click is not on a row."""
    if y < 0 or n_rows <= 0:
        return None
    row = int(y) // max(1, int(row_px))
    if 0 <= row < n_rows:
        return row
    return None


def _query_peer(query_control: Any, label: str = "Ask") -> Any:
    get_peer = getattr(query_control, "getPeer", None)
    if not callable(get_peer):
        _ovlog("%s getPeer missing query=%s", label, type(query_control).__name__)
        return None
    peer = uno_probe(get_peer, None)
    if peer is None:
        _ovlog("%s getPeer failed", label, exc_info=True)
        return None
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
    parent = (
        _query_peer(parent_control, "dialog") if parent_control is not None else None
    ) or _query_peer(query_control)
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
    qr = query_control.getPosSize()
    _x, _y, w, h = _above_ready_rect(qr, _POPUP_MAX_ROWS, status_top=_status_top(send_listener))

    def _make_desc(service: str, win_type: int, p: Any, attrs: int) -> Any:
        desc = descriptor_cls()
        desc.Type = win_type
        desc.WindowServiceName = service
        desc.Parent = p
        desc.ParentIndex = -1
        desc.Bounds = rectangle_cls(0, 0, w, h)
        desc.WindowAttributes = attrs
        return desc

    host_desc = _make_desc("floatingwindow", top, parent, 0)
    _ovlog("createWindow floatingwindow %sx%s", w, h)
    try:
        host = toolkit.createWindow(host_desc)
    except Exception:
        _ovlog("createWindow floatingwindow FAILED", exc_info=True)
        host = None
    if host is None:
        log.warning("slash popup: overlay window could not be created")
        return None
    desc = _make_desc("listbox", simple, host, child_attrs)
    try:
        win = toolkit.createWindow(desc)
    except Exception:
        _ovlog("createWindow listbox FAILED", exc_info=True)
        win = None
    # Ask control, not its peer: controls stay accessible on LibreOffice 26.2.
    ctx = getattr(send_listener, "ctx", None)
    offset = _overlay_offset(host, query_control, qr, ctx) if win is not None else None
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
            return bool(host.handle_key(code, mods, ch, from_overlay=False))

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
    _open: bool
    _selected: int

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
        self._popup_floater: Any = None
        # Dialog-local -> overlay host offset from _overlay_offset.
        self._ask_origin: tuple[int, int] | None = None
        self._row_px: int = _POPUP_ROW_PX
        self.control: Any = None
        self._key_handlers: dict[str, tuple[Any, Any]] = {}
        self._open = False
        self._matches: list[SlashCommand] = []
        self._selected = 0
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
        elif query_has_peer and ENABLE_SLASH:
            # The sidebar FocusManager consumes Esc in panel content (focus
            # moves to the panel title). It is an Application key listener
            # too, so ours must be registered first, at panel creation, and
            # stay registered: re-adding it on each show put it behind.
            self._attach_toolkit_keys()

    def dispose(self) -> None:
        """Panel teardown: close the popup and drop the toolkit key handler."""
        self.hide(restore_send=False)
        self._detach_keys("toolkit")

    def _bind_overlay(self) -> None:
        if self._popup_floater is not None:
            return
        created = _create_ask_peer_listbox(self.query_control, self._overlay_parent, send_listener=self.send_listener)
        if created is None:
            return
        win, floater, origin = created
        self._popup_floater = floater
        self.control = win
        self._ask_origin = origin
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
        _ovlog("hide open_was=%s toolkit=%s", self._open, self._popup_floater is not None)
        self._detach_keys("frame")
        self._open = False
        self._matches = []
        self._selected = 0
        set_control_visible(self._placeholder, False)
        if self._popup_floater is not None:
            ctrl = self.control
            floater = self._popup_floater
            set_control_visible(ctrl, False)
            set_control_visible(floater, False)
            for win, msg in (
                (ctrl, "slash popup: hide dispose failed"),
                (floater, "slash popup: hide dispose floater failed"),
            ):
                if callable(getattr(win, "dispose", None)):
                    best_effort(win.dispose, msg)
            self._popup_floater = None
            self.control = None
            self._ask_origin = None
        else:
            set_control_visible(self.control, False)
        if restore_send:
            self._dispatch_query_has_text()

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
        matches = filter_slash_commands(text, load_slash_lru())
        _ovlog("on_query_text matches=%s names=%s", len(matches), [c.name for c in matches[:8]])
        if not matches:
            _ovlog("on_query_text hide (no matches)")
            _defer_to_next_turn(self.hide)
            return
        # Never createWindow/setVisible inside Ask textChanged — returning from that
        # callback after a mapped TOP overlay deadlocks VCL and eats later keystrokes.
        new_names = [c.name for c in matches]
        if self._open and new_names == self.visible_names:
            _ovlog("on_query_text same matches skip recreate")
            return
        _defer_to_next_turn(self._show_matches, matches)
        _ovlog("on_query_text posted show n=%s", len(matches))

    def _show_matches(self, matches: list[SlashCommand]) -> None:
        _ovlog(
            "show_matches bind_floater=%s control=%s",
            self._popup_floater is not None,
            self.control is not None,
        )
        if self._popup_floater is not None:
            # addItems/setPosSize on a mapped TOP window deadlocks; recreate via fill-before-show.
            # Not a dismiss: do not TEXT_UPDATED while the next overlay is about to map.
            _ovlog("show_matches recreate")
            self.hide(restore_send=False)
        self._bind_overlay()
        _ovlog("after bind control=%s toolkit=%s", self.control is not None, self._popup_floater is not None)
        if self.control is None:
            _ovlog("on_query_text NO overlay list; slash prefix ignored")
            log.warning("slash popup: no overlay list; slash prefix ignored")
            return
        self._matches = matches
        self._selected = 0
        self._open = True
        self.reposition()
        self._fill_visible_list()
        _ovlog("filled before show")
        host = self._popup_floater
        if host is not None:
            set_control_visible(host, True)
        set_control_visible(self.control, True)
        # Do not reposition() after setVisible. reposition getPosSize's Ask, and
        # once this TOP overlay is mapped that deadlocks VCL (panel.py
        # QueryTextListener). The pre-show reposition above is the one that counts.
        self._bring_overlay_front()
        if self._popup_floater is not None:
            self._attach_click()
            self._attach_keys()
        self._attach_frame_keys()
        self._attach_toolkit_keys()
        # TOP host steals caret unless we return it — keep typing in Ask.
        def _focus_ask(label: str) -> bool:
            sf = getattr(self.query_control, "setFocus", None)
            if callable(sf):
                with suppress_disposed(f"slash popup {label}", logger=log):
                    sf()
                    _ovlog(f"Ask setFocus {label}")
                return True
            return False

        if _focus_ask("after TOP show"):
            _defer_to_next_turn(_focus_ask, "idle refocus")
        _ovlog("show complete TOP-above-Ready floater=%s", host is not None)

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
        self.accept_selected()
        return True

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
            status_top = _status_top(self.send_listener)
            if self._popup_floater is not None:
                height = _list_height(ctrl, rows)
                one = _list_height(ctrl, 1)
                self._row_px = max(1, (height - one) // (rows - 1)) if rows > 1 else one
                x, y, w, h = _above_ready_rect(qr, rows, status_top=status_top, height=height)
            else:
                x, y, w, h = _above_ready_rect(qr, rows, status_top=status_top)
            _ovlog(
                "reposition Ask=%sx%s@%s,%s popup_bounds=%s,%s %sx%s rows=%s toolkit=%s origin=%s",
                qr.Width,
                qr.Height,
                qr.X,
                qr.Y,
                x,
                y,
                w,
                h,
                rows,
                self._popup_floater is not None,
                self._ask_origin,
            )
            if self._popup_floater is not None:
                floater = self._popup_floater
                ox, oy = self._ask_origin or (0, 0)
                floater.setPosSize(x + ox, y + oy, w, h, _POS_SIZE_FLAGS)
                ctrl.setPosSize(0, 0, w, h, _POS_SIZE_FLAGS)
                return
            # Mock list in unit tests: tall rectangle above Ask, never 14px closed.
            ctrl.setPosSize(int(qr.X), max(16, int(y)), w, h, _POS_SIZE_FLAGS)

    def _fill_visible_list(self) -> None:
        ctrl = self.control
        if ctrl is None:
            return
        labels = tuple(format_slash_item(cmd) for cmd in self._matches)
        if self._popup_floater is not None:
            add = getattr(ctrl, "addItems", None)
            if callable(add):
                try:
                    add(labels, 0)
                    _ovlog("fill_visible addItems after show ok")
                except Exception:
                    log.warning("slash popup: fill addItems failed", exc_info=True)
            return
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

    def _attach_key_handler(self, target: Any, label: str) -> Any:
        if target is None:
            return None
        add = getattr(target, "addKeyHandler", None)
        if not callable(add):
            _ovlog("%s key handler: no addKeyHandler", label)
            return None
        handler = _make_slash_key_handler(self, label)
        if handler is None:
            return None
        try:
            add(handler)
            _ovlog("%s key handler attached", label)
            return handler
        except Exception:
            log.warning("slash popup: %s key handler attach failed", label, exc_info=True)
            return None

    def _attach_frame_keys(self) -> None:
        """Catch Esc/Enter even when the TOP overlay or document has focus."""
        if "frame" in self._key_handlers:
            return
        frame = getattr(self.send_listener, "frame", None)
        get_controller = getattr(frame, "getController", None) if frame is not None else None
        if not callable(get_controller):
            _ovlog("frame key handler: no controller")
            return
        controller = uno_probe(get_controller, None)
        handler = self._attach_key_handler(controller, "frame")
        if handler is not None:
            self._key_handlers["frame"] = (controller, handler)

    def _attach_toolkit_keys(self) -> None:
        """Esc/Enter/arrows for the open popup, ahead of the sidebar FocusManager (see __init__)."""
        if "toolkit" in self._key_handlers:
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
        handler = self._attach_key_handler(tk, "toolkit")
        if handler is not None:
            self._key_handlers["toolkit"] = (tk, handler)

    def _detach_keys(self, kind: str) -> None:
        target, handler = self._key_handlers.pop(kind, (None, None))
        _detach_key_handler(target, handler, kind)

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
                if host.handle_key(
                    int(getattr(e, "KeyCode", 0) or 0),
                    int(getattr(e, "Modifiers", 0) or 0),
                    getattr(e, "KeyChar", None),
                    from_overlay=True,
                ):
                    with suppress_disposed("slash list Consume", logger=log):
                        if hasattr(e, "Consume"):
                            setattr(e, "Consume", True)

        ctrl = self.control
        if ctrl is None or not hasattr(ctrl, "addKeyListener"):
            _ovlog("key listener skip overlay")
            return
        try:
            ctrl.addKeyListener(_Keys())
            _ovlog("key listener attached overlay")
        except Exception:
            log.warning("slash popup: key listener attach failed overlay", exc_info=True)
