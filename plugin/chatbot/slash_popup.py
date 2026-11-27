# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Steerable slash-command completion menu attached to the sidebar Ask field.

A native ``PopupMenu`` is modal — the user could not keep typing. The XDL
placeholder is ``dlg:menulist`` (LibreOffice dialog.dtd has no ``listbox``).

A sibling control inside the dialog cannot paint over the rich-text
transcript, so the list lives in its own overlap window.
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
from typing import Any, cast

from plugin.chatbot.dialogs import get_control_text, set_control_text, set_control_visible
from plugin.chatbot.slash_commands import (
    SlashCommand,
    classify_slash_key,
    filter_slash_commands,
    format_slash_popup_item,
    load_slash_lru,
    parse_slash_text,
    run_slash_command,
    slash_ask_past_command_name,
    slash_display_name,
    slash_typed_prefix,
    SlashResult,
)
from plugin.framework.errors import best_effort, suppress_disposed, uno_read
from plugin.framework.uno_listeners import BaseKeyHandler, BaseKeyListener, BaseMouseListener

log = logging.getLogger(__name__)


def _defer_to_next_turn(fn: Any, *args: Any) -> None:
    """Run ``fn`` on a later main-thread turn. Do not call it inline on failure.

    Accept and Esc must not reach ``hide()`` on the listener stack.
    ``accept_selected`` → ``run_slash_command`` → ``hide()`` (and Esc →
    ``hide()``) ``dispose()`` the toolkit window from inside its own
    ``XKeyHandler`` / ``XMouseListener`` while VCL is still in that callback.
    ``main.post`` enqueues once AsyncCallback exists (the headed
    sidebar). The listener returns before dispose. An inline fallback is
    the same deadlock, so a failed post is only logged.
    """
    try:
        from plugin.framework.queue_executor import main

        main.post(fn, *args)
    except Exception:
        log.warning("slash popup: could not defer main-thread turn", exc_info=True)


def _dispose(obj: Any, msg: str) -> None:
    if callable(getattr(obj, "dispose", None)):
        best_effort(obj.dispose, msg)


# Feature flag: set False only to park the overlay without removing wiring.
ENABLE_SLASH = True

_POPUP_MAX_ROWS = 6
_POPUP_ROW_PX = 14
_POS_SIZE_FLAGS = 15  # X + Y + WIDTH + HEIGHT


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
    size = uno_read(lambda: fn(0, n), None) if callable(fn) else None
    height = getattr(size, "Height", None)
    if isinstance(height, int) and height > 0:
        return height
    return _overlay_height(n)


def _hscroll_height(ctrl: Any, rows: int, entries: int, width: int) -> int:
    """Extra height for the list's horizontal scrollbar, or 0 when none shows.

    The toolkit ``listbox`` is always ``WB_AUTOHSCROLL`` (vclxtoolkit.cxx),
    and ``CalcBlockSize(0, n)`` sizes n rows with no scrollbar. A row wider
    than the list makes ImplListBox show an HScroll inside those n rows, so a
    single match was half hidden (QA B7). ``CalcBlockSize(1, n)`` is one 'X'
    wide, so it adds VCL's own ScrollBarSize (HiDPI-scaled, native theme) to
    the height. Call after ``addItems``: the widths come from the entries.
    Text width is read with lines > entries; with fewer lines CalcBlockSize
    pads the width by a VScroll (by a scrollbar even at 1 of 1 rows).
    """
    fn = getattr(ctrl, "getMinimumSize", None)
    if not callable(fn):
        return 0
    n = min(max(rows, 1), _POPUP_MAX_ROWS)
    full = uno_read(lambda: fn(0, n), None)
    narrow = uno_read(lambda: fn(1, n), None)
    text = uno_read(lambda: fn(0, max(entries, n) + 1), None)
    sizes = (getattr(text, "Width", None), getattr(full, "Height", None), getattr(narrow, "Height", None))
    if not all(isinstance(v, int) for v in sizes):
        return 0
    full_w, full_h, narrow_h = cast("tuple[int, int, int]", sizes)
    bar = max(0, narrow_h - full_h)
    # More matches than rows: the VScroll takes the same width from the rows.
    avail = int(width) - (bar if entries > n else 0)
    return bar if full_w > avail else 0


def _printable_key_char(key_char: Any) -> str | None:
    if key_char is None:
        return None
    raw: Any = key_char
    if not isinstance(raw, str):
        # A UNO char arrives as uno.Char (pyuno_runtime.cxx Runtime::any2PyObject, TypeClass_CHAR).
        raw = key_char.value
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
    return status.getPosSize().Y


def _location_on_screen(obj: Any) -> tuple[int, int] | None:
    """Screen origin of an AWT window/control, or None."""
    if obj is None:
        return None
    get_acc = getattr(obj, "getAccessibleContext", None)
    if not callable(get_acc):
        return None
    acc = uno_read(get_acc, None)
    fn = getattr(acc, "getLocationOnScreen", None) if acc is not None else None
    if not callable(fn):
        return None
    pt = uno_read(fn, None)
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
    origin = uno_read(lambda: _host_origin(host, ctx), None)
    ask_xy = _location_on_screen(ask)
    if origin is None or ask_xy is None:
        return None
    return (
        ask_xy[0] - origin[0] - qr.X,
        ask_xy[1] - origin[1] - qr.Y,
    )


def _above_ready_rect(
    qr: Any,
    rows: int,
    *,
    status_top: int | None,
    height: int | None = None,
) -> tuple[int, int, int, int]:
    """Dialog-local rect: ~6 rows above Ready (over transcript), else classic above Ask."""
    width = max(20, qr.Width)
    h = height if height is not None else _overlay_height(rows)
    x = qr.X
    if status_top is not None:
        y = status_top - h - 2
        return x, y, width, h
    return x, qr.Y - h - 2, width, h


def _row_index_at_y(y: int, n_rows: int, row_px: int = _POPUP_ROW_PX) -> int | None:
    """List row under a mouse Y, or None when the click is not on a row."""
    if y < 0 or n_rows <= 0:
        return None
    row = int(y) // max(1, int(row_px))
    if 0 <= row < n_rows:
        return row
    return None


def _query_peer(query_control: Any) -> Any:
    get_peer = getattr(query_control, "getPeer", None)
    if not callable(get_peer):
        log.debug("slash popup: getPeer missing query=%s", type(query_control).__name__)
        return None
    return uno_read(get_peer, None)


def _toolkit_for_peer(peer: Any) -> Any:
    if peer is not None and hasattr(peer, "getToolkit"):
        return uno_read(peer.getToolkit, None)
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
) -> tuple[Any, Any, tuple[int, int]] | None:
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
        _query_peer(parent_control) if parent_control is not None else None
    ) or _query_peer(query_control)
    if parent is None:
        return None
    toolkit = _toolkit_for_peer(parent)
    if toolkit is None or not hasattr(toolkit, "createWindow"):
        log.debug("slash popup: no toolkit on overlay parent")
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
    try:
        host = toolkit.createWindow(host_desc)
    except Exception:
        log.debug("createWindow floatingwindow FAILED", exc_info=True)
        host = None
    if host is None:
        log.warning("slash popup: overlay window could not be created")
        return None
    desc = _make_desc("listbox", simple, host, child_attrs)
    try:
        win = toolkit.createWindow(desc)
    except Exception:
        log.debug("createWindow listbox FAILED", exc_info=True)
        win = None
    # Ask control, not its peer: controls stay accessible on LibreOffice 26.2.
    ctx = getattr(send_listener, "ctx", None)
    offset = _overlay_offset(host, query_control, qr, ctx) if win is not None else None
    if win is None or offset is None:
        log.warning("slash popup: overlay list or its position unavailable")
        for obj in (win, host):
            _dispose(obj, "slash popup: overlay dispose failed")
        return None
    return win, host, offset


def _make_slash_key_handler(host: Any, label: str) -> BaseKeyHandler:
    class _Handler(BaseKeyHandler):
        def on_key_pressed(self, e: Any) -> bool:
            code = e.KeyCode
            mods = e.Modifiers
            ch = e.KeyChar
            return host.handle_key(code, mods, ch, from_overlay=False)

    return _Handler()


class SlashPopupController:
    """Show, filter, and accept slash commands on the Ask-field overlay list."""

    query_control: Any
    send_listener: Any
    _overlay_parent: Any
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
        # The XDL placeholder is only hidden. Keys go to the toolkit handler,
        # not to that control.
        query_has_peer = callable(getattr(query_control, "getPeer", None))
        if query_has_peer and ENABLE_SLASH:
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

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def visible_names(self) -> list[str]:
        return [cmd.name for cmd in self._matches]

    @property
    def selected_command(self) -> SlashCommand | None:
        if not self._matches or not (0 <= self._selected < len(self._matches)):
            return None
        return self._matches[self._selected]

    @property
    def selected_name(self) -> str | None:
        cmd = self.selected_command
        return cmd.name if cmd is not None else None

    def hide(self, *, restore_send: bool = True) -> None:
        """Close the popup. Callers inside a key/mouse callback must defer this.

        ``restore_send`` dispatches ``TEXT_UPDATED`` from the live Ask text
        after the toolkit window is disposed. Recreate passes False so a
        slash draft does not run UpdateUI while the next overlay is mapped.
        """
        self._detach_keys("frame")
        self._open = False
        self._matches = []
        self._selected = 0
        if self._popup_floater is not None:
            ctrl = self.control
            floater = self._popup_floater
            set_control_visible(ctrl, False)
            set_control_visible(floater, False)
            _dispose(ctrl, "slash popup: hide dispose failed")
            _dispose(floater, "slash popup: hide dispose floater failed")
            self._popup_floater = None
            self.control = None
            self._ask_origin = None
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

    def on_query_text(self, text: str) -> bool:
        """Open or narrow the popup from the current Ask-box contents. Returns True if TEXT_UPDATED should be skipped."""
        if not ENABLE_SLASH:
            return False
        prefix = slash_typed_prefix(text)
        if slash_ask_past_command_name(text):
            _defer_to_next_turn(self.hide)
            return False
        if prefix is None:
            # hide on the textChanged stack can deadlock VCL when main.post is full; defer to next turn.
            _defer_to_next_turn(self.hide)
            return False
        matches = filter_slash_commands(
            text,
            load_slash_lru(),
            getattr(self.send_listener, "cached_doc_type", None),
        )
        if not matches:
            _defer_to_next_turn(self.hide)
        elif not (self._open and matches == self._matches):
            _defer_to_next_turn(self._show_matches, matches)
        return True

    def _show_matches(self, matches: list[SlashCommand]) -> None:
        if self._popup_floater is not None:
            # addItems/setPosSize on a mapped TOP window deadlocks; recreate via fill-before-show.
            # Not a dismiss: do not TEXT_UPDATED while the next overlay is about to map.
            self.hide(restore_send=False)
        self._bind_overlay()
        if self.control is None:
            log.warning("slash popup: no overlay list; slash prefix ignored")
            return
        self._matches = matches
        self._selected = 0
        self._open = True
        # Fill before reposition: the HScroll check measures the entries.
        # Both run before setVisible (addItems/setPosSize on a mapped host deadlock).
        self._fill_visible_list()
        self.reposition()
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
                return True
            return False

        if _focus_ask("after TOP show"):
            _defer_to_next_turn(_focus_ask, "idle refocus")

    def handle_key(
        self,
        key_code: int,
        modifiers: int = 0,
        key_char: Any = None,
        *,
        from_overlay: bool = False,
    ) -> bool:
        """True when the popup consumed the key (do not Send / insert newline)."""
        if not ENABLE_SLASH:
            return False
        if not self._open:
            return False
        action = classify_slash_key(key_code, modifiers)
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
        if not self._matches:
            return
        name = slash_display_name(self._matches[self._selected])
        set_control_text(self.query_control, "/" + name)
        self.on_query_text("/" + name)

    def accept_selected(self) -> None:
        host = self.send_listener
        raw = get_control_text(self.query_control) or ""
        parsed = parse_slash_text(raw.strip())
        # Exact "/explain" + Enter runs the command; it does not complete to "/explain ".
        # The same is true for the active locale's alias ("/解説" when that is explain).
        if parsed is not None:
            cmd, arg = parsed
            if not slash_ask_past_command_name(raw):
                # Send-turn commands return CONTINUE_SEND so Send runs _do_send.
                # Without SEND_CLICKED the draft stays in Ask until a second Enter.
                def _accept_exact_or_send() -> None:
                    from plugin.chatbot.send_state import SendEvent, SendEventKind

                    result = run_slash_command(cmd, host, arg, from_popup=False)
                    if result is SlashResult.CONTINUE_SEND:
                        # SEND_COMPLETED clears has_text; exact /preset + Enter
                        # must sync Ask before SEND_CLICKED or send_state ignores the click.
                        host.sync_has_text_from_query()
                        host.dispatch(SendEvent(SendEventKind.SEND_CLICKED))

                _defer_to_next_turn(_accept_exact_or_send)
                return
        cmd_obj = self.selected_command
        if cmd_obj is None:
            return
        arg = parsed[1] if parsed is not None and parsed[0].name == cmd_obj.name else ""
        # from_popup=True completes "/name " for presets instead of
        # running them with an empty argument from the partial filter text.
        _defer_to_next_turn(lambda: run_slash_command(cmd_obj, host, arg, from_popup=True))

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
        """Raise the toolkit listbox above Ready/transcript siblings."""
        for label, obj in (
            ("popup_floater", self._popup_floater),
            ("control", self.control),
        ):
            if obj is None:
                continue
            to_front = getattr(obj, "toFront", None)
            if not callable(to_front):
                continue
            best_effort(to_front, f"toFront failed {label}")

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
            height = _list_height(ctrl, rows)
            one = _list_height(ctrl, 1)
            self._row_px = max(1, (height - one) // (rows - 1)) if rows > 1 else one
            height += _hscroll_height(ctrl, rows, len(self._matches), max(20, int(qr.Width)))
            x, y, w, h = _above_ready_rect(qr, rows, status_top=status_top, height=height)
            floater = self._popup_floater
            if floater is not None and self._ask_origin is not None:
                ox, oy = self._ask_origin
                floater.setPosSize(x + ox, y + oy, w, h, _POS_SIZE_FLAGS)
                ctrl.setPosSize(0, 0, w, h, _POS_SIZE_FLAGS)

    def _fill_visible_list(self) -> None:
        ctrl = self.control
        if ctrl is None:
            return
        labels = tuple(format_slash_popup_item(cmd) for cmd in self._matches)
        add = getattr(ctrl, "addItems", None)
        if callable(add):
            try:
                add(labels, 0)
            except Exception:
                log.warning("slash popup: fill addItems failed", exc_info=True)

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
            return None
        handler = _make_slash_key_handler(self, label)
        try:
            add(handler)
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
            return
        controller = uno_read(get_controller, None)
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
            return
        handler = self._attach_key_handler(tk, "toolkit")
        if handler is not None:
            self._key_handlers["toolkit"] = (tk, handler)

    def _detach_keys(self, kind: str) -> None:
        target, handler = self._key_handlers.pop(kind, (None, None))
        if target is None or handler is None:
            return
        rem = getattr(target, "removeKeyHandler", None)
        if callable(rem):
            best_effort(lambda: rem(handler), f"{kind} key handler remove failed")

    def _attach_click(self) -> None:
        ctrl = self.control
        if ctrl is None or not hasattr(ctrl, "addMouseListener"):
            return

        host = self

        class _Click(BaseMouseListener):
            def mouseReleased(self, e: Any) -> None:  # noqa: N802 -- UNO signature
                host.accept_row_at_y(e.Y)

        try:
            ctrl.addMouseListener(_Click())
        except Exception:
            log.warning("slash popup: mouse listener attach failed", exc_info=True)
        # skip item/action: addItemListener deadlocks idle addItems/show

    def _attach_keys(self) -> None:
        """Forward Esc/Enter/arrows when the list stole focus from Ask."""
        host = self

        class _Keys(BaseKeyListener):
            def keyPressed(self, e: Any) -> None:  # noqa: N802 -- UNO signature
                host.handle_key(e.KeyCode, e.Modifiers, e.KeyChar, from_overlay=True)

        ctrl = self.control
        if ctrl is None or not hasattr(ctrl, "addKeyListener"):
            return
        try:
            ctrl.addKeyListener(_Keys())
        except Exception:
            log.warning("slash popup: key listener attach failed overlay", exc_info=True)
