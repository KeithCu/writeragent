# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc cell navigation: canonical ``Sheet1.B2`` addresses, ``cell://`` links, span registry, mouse listener."""

from __future__ import annotations

import logging
import re
import time
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import unquote
import uno

from plugin.calc.address_utils import MAX_COL, MAX_ROW, format_address, parse_address, qualify_a1, split_sheet_prefix
from plugin.calc.calc_utils import query_interface, resolve_sheet_and_cell
from plugin.framework.errors import best_effort
from plugin.framework.uno_context import uno_same
from plugin.framework.uno_listeners import BaseMouseListener, BaseStatusListener

log = logging.getLogger(__name__)

_XACCESSIBLE_TEXT = "com.sun.star.accessibility.XAccessibleText"

_CELL_HREF_RE = re.compile(r"""href\s*=\s*(["'])(?:cell://|writeragent-cell://)([^"']+)\1""", re.IGNORECASE)
WRITERAGENT_CELL_URL_PREFIX = "writeragent-cell://"

_CELL_LINK_LISTENERS: dict[int, Any] = {}


@dataclass
class CellLinkSpanRegistry:
    """Maps sidebar RichTextControl character offsets to Calc cell addresses.

    Sidebar callers use ``cell_link_registry.add``, ``clear``, and
    ``drop_from`` directly.
    """

    _spans: dict[int, list[tuple[int, int, str]]] = field(default_factory=dict)
    _refs: dict[int, weakref.ref[Any]] = field(default_factory=dict, repr=False)

    def _evict(self, key: int, ref: weakref.ref[Any]) -> None:
        # Only evict if this weakref is still the active entry for key, so a
        # stale callback cannot drop spans registered for a new object at the same id.
        if self._refs.get(key) is ref:
            self._refs.pop(key, None)
            self._spans.pop(key, None)

    def clear(self, control: Any) -> None:
        key = id(control)
        self._spans.pop(key, None)
        self._refs.pop(key, None)

    def add(self, control: Any, start: int, end: int, address: str) -> None:
        if control is None or start >= end or not address:
            return
        key = id(control)
        if key not in self._refs:
            def _on_gc(ref: weakref.ref[Any], k: int = key) -> None:
                self._evict(k, ref)

            try:
                self._refs[key] = weakref.ref(control, _on_gc)
            except TypeError:
                # A control that cannot be weak-referenced keeps its spans until clear.
                pass
        self._spans.setdefault(key, []).append((start, end, address))

    def lookup(self, control: Any, index: int) -> str | None:
        if control is None or index < 0:
            return None
        for start, end, addr in self._spans.get(id(control), []):
            if start <= index < end:
                return addr
        return None

    def drop_from(self, control: Any, start_len: int) -> None:
        """Drop spans that end past a truncation point.

        A failed formatted copy rolls the control back to the length it had
        before the separator. Spans recorded for that copy, including one
        that starts before the cut and ends inside the deleted tail, would
        otherwise resolve a click into text that is gone.
        """
        key = id(control)
        spans = self._spans.get(key)
        if not spans:
            return
        self._spans[key] = [span for span in spans if span[1] <= start_len]


cell_link_registry = CellLinkSpanRegistry()


def _normalize_a1(a1: str) -> tuple[int, int] | None:
    """Parse one A1 cell (``B2`` or ``$B$2``) to ``(col, row)``, or *None* if invalid."""
    # parse_address has a @deal.pre string bound; a long or non-ASCII href
    # is external input and must return None, not raise, under pytest.
    if not a1 or not a1.isascii() or len(a1) > 32:
        return None
    try:
        col, row = parse_address(a1)
    except Exception:
        return None
    if not (0 <= col <= MAX_COL and 0 <= row <= MAX_ROW):
        return None
    return col, row


def normalize_cell_address(raw: str) -> str | None:
    """Return a canonical cell or range address (``Sheet1.B2``, ``B2``, ``Sheet1.A1:B5``) or *None*."""
    text = raw.strip()
    for prefix in ("cell://", WRITERAGENT_CELL_URL_PREFIX):
        if text.lower().startswith(prefix):
            # Writer's HTML import percent-encodes link targets (SwHTMLParser::NewAnchor,
            # sw/source/filter/html/htmlgrin.cxx, via URIHelper::SmartRel2Abs). A portion's
            # URL for My Sheet.A1 reads My%20Sheet.A1, and without decoding the click looks
            # for a sheet that does not exist.
            text = unquote(text[len(prefix) :]).strip()
            break
    if not text:
        return None
    sheet, a1 = split_sheet_prefix(text)
    start, sep, end = a1.partition(":")
    first = _normalize_a1(start)
    if first is None:
        return None
    addr = format_address(*first)
    if sep:
        # A range end may repeat the sheet (Sheet2.A1:Sheet2.B5); the start's sheet wins.
        last = _normalize_a1(split_sheet_prefix(end)[1])
        if last is None:
            return None
        if last != first:
            top_left = (min(first[0], last[0]), min(first[1], last[1]))
            bottom_right = (max(first[0], last[0]), max(first[1], last[1]))
            addr = f"{format_address(*top_left)}:{format_address(*bottom_right)}"
    if sheet is None:
        return addr
    # Keep a plain name plain: link addresses are navigation keys, not formula
    # text, and the sidebar HTML link regexes in plugin/chatbot/rich_text_paste.py
    # and _CELL_HREF_RE accept `Sheet Name.B2` but not a quote inside the
    # address. qualify_a1 quotes many more names (hyphens, a leading digit) for
    # formulas, and those names must stay unquoted here; quote only names that
    # would otherwise split (dot/bang/apostrophe) or lose a leading $.
    if sheet.startswith("$") or any(ch in sheet for ch in ".!'"):
        return qualify_a1(sheet, addr)
    return f"{sheet}.{addr}"


def navigate_to_cell(doc: Any, address: str) -> bool:
    """Select *address* (a cell, or a whole ``A1:B5`` range) in the Calc document (PyUNO select, not sidebar dispatch)."""
    normalized = normalize_cell_address(address) or ""
    head, sep, tail = normalized.rpartition(":") if ":" in split_sheet_prefix(normalized)[1] else (normalized, "", "")
    resolved = resolve_sheet_and_cell(doc, head)
    if not resolved:
        log.debug("navigate_to_cell: could not resolve %r", address)
        return False
    sheet, col, row = resolved
    end = _normalize_a1(tail) if sep else None
    try:
        controller = doc.getCurrentController()
        if controller is None:
            return False
        if controller.getActiveSheet().getName() != sheet.getName():
            controller.setActiveSheet(sheet)
        if end is not None:
            target = sheet.getCellRangeByPosition(col, row, end[0], end[1])
        else:
            target = sheet.getCellByPosition(col, row)
        controller.select(target)
        return True
    except Exception:
        log.exception("navigate_to_cell failed for %r", address)
        return False


def cell_ref_at_index(text: str, index: int) -> str | None:
    """Return a cell address if *index* falls inside a ``cell://`` HTML link in *text*."""
    if not text or index < 0:
        return None
    for match in _CELL_HREF_RE.finditer(text):
        start, end = match.span()
        if start <= index <= end:
            return normalize_cell_address(match.group(2))
    prefix = WRITERAGENT_CELL_URL_PREFIX
    lower = text.lower()
    pos = lower.rfind(prefix, 0, min(index + 1, len(text)))
    while pos >= 0:
        rest = text[pos + len(prefix) :]
        end = 0
        while end < len(rest) and rest[end] not in "\"'<> \n\r\t)":
            end += 1
        if end > 0:
            addr = normalize_cell_address(rest[:end])
            if addr and pos <= index <= pos + len(prefix) + end:
                return addr
        pos = lower.rfind(prefix, 0, pos)
    return None


def lookup_cell_ref_at_index(control: Any, index: int) -> str | None:
    """Resolve a cell address at *index* using the span registry, then plain text."""
    addr = cell_link_registry.lookup(control, index)
    if addr:
        return addr
    return cell_ref_at_index(control.getText(), index)


def _accessible_text(control: Any) -> Any | None:
    def _lookup() -> Any | None:
        ctx = control.getAccessibleContext()
        if ctx is None:
            return None
        ax = query_interface(ctx, _XACCESSIBLE_TEXT)
        if ax:
            return ax
        for i in range(ctx.getAccessibleChildCount()):
            child = ctx.getAccessibleChild(i)
            if child is None:
                continue
            child_ctx = child.getAccessibleContext()
            if child_ctx is not None:
                ax = query_interface(child_ctx, _XACCESSIBLE_TEXT)
                if ax:
                    return ax
        return None

    return best_effort(_lookup, "cell link: accessible text lookup failed")


def _char_index_at_point(control: Any, x: int, y: int) -> int | None:
    axtext = _accessible_text(control)
    if axtext is None:
        return None
    from com.sun.star.awt import Point

    idx = best_effort(lambda: axtext.getIndexAtPoint(Point(x, y)), "cell link: getIndexAtPoint failed")
    if isinstance(idx, int) and idx >= 0:
        return idx
    return None


def _click_text_index(control: Any, x: int, y: int) -> int | None:
    """Character index under the mouse, preferring the control caret after click."""
    idx = getattr(control.getModel(), "SelectionStart", None)
    if isinstance(idx, int) and idx >= 0:
        return idx
    return _char_index_at_point(control, x, y)


# The sidebar RichTextControl (forms ORichTextPeer over an EditView) exposes no
# caret offset to UNO and no XAccessibleText, so a click cannot be mapped to a
# character index. Its attribute dispatchers do report the character attributes at
# the caret: RichTextViewPort::MouseButtonDown places the caret and the peer's
# status listeners get the new state. Each link span is tagged with its address in
# the Asian font style name, which never changes how the ASCII link text renders.
CELL_LINK_FONT_PROPERTY = "CharFontStyleNameAsian"
CELL_LINK_FONT_PREFIX = "WriterAgentCell:"
_CELL_LINK_STATUS_URL = ".uno:CharFontNameCJK"
# A press and release further apart than this is a drag-select, not a click.
_CLICK_SLOP_PX = 4
# How long a click waits for a caret status that arrives after the release.
_PENDING_CLICK_SEC = 0.5


def mark_cell_link_range(text_range: Any, address: str | None) -> None:
    """Tag *text_range* in the sidebar control so a click on it resolves to *address*.

    ``None`` clears the tag (used on the insert cursor right after a link).
    """
    if text_range is None:
        return
    value = CELL_LINK_FONT_PREFIX + address if address else ""
    best_effort(lambda: text_range.setPropertyValue(CELL_LINK_FONT_PROPERTY, value), "cell link: tag range")


def cell_address_from_font_state(state: Any) -> str | None:
    """Read the tagged address from a ``.uno:CharFontNameCJK`` status ``State``."""
    values = state if isinstance(state, (tuple, list)) else (state,)
    for prop in values:
        if not str(getattr(prop, "Name", "")).endswith(".StyleName"):
            continue
        value = getattr(prop, "Value", None)
        if isinstance(value, str) and value.startswith(CELL_LINK_FONT_PREFIX):
            return normalize_cell_address(value[len(CELL_LINK_FONT_PREFIX) :])
    return None


def _add_caret_font_status_listener(control: Any, listener: Any) -> Any | None:
    """Register *listener* for the caret's Asian font on the rich peer; return the dispatch."""
    # A plain UnoControlEdit peer (the fallback when the rich control is
    # unavailable) is not an XDispatchProvider, so it has no caret status.
    peer = control.getPeer()
    if peer is None or not hasattr(peer, "queryDispatch"):
        return None
    # ORichTextPeer::queryDispatch matches on URL.Complete only.
    url = uno.createUnoStruct("com.sun.star.util.URL", Complete=_CELL_LINK_STATUS_URL)
    dispatch = peer.queryDispatch(url, "", 0)
    if dispatch is None:
        return None
    dispatch.addStatusListener(listener, url)
    return dispatch


def attach_calc_cell_link_listener(control: Any, get_calc_doc: Callable[[], Any | None]) -> None:
    """Attach mouse and caret-status listeners on *control* for cell link navigation."""
    if control is None:
        return
    ctrl_id = id(control)
    if ctrl_id in _CELL_LINK_LISTENERS:
        return
    _MB_LEFT = 1  # com.sun.star.awt.MouseButton.LEFT

    def _navigate(addr: str) -> None:
        doc = get_calc_doc()
        if doc is None:
            log.debug("cell link click: no Calc document for %r", addr)
            return
        if navigate_to_cell(doc, addr):
            log.debug("cell link navigated to %r", addr)

    class _CalcCellLinkMouseListener(BaseMouseListener, BaseStatusListener):
        def __init__(self) -> None:
            super().__init__()
            self.caret_addr: str | None = None
            self.press: tuple[int, int] | None = None
            self.pending_until: float = 0.0
            self.status_dispatch: Any = None

        def disposing(self, Source: Any) -> None:
            if self.status_dispatch is not None and uno_same(Source.Source, self.status_dispatch):
                self.status_dispatch = None
                self.caret_addr = None
                return
            _CELL_LINK_LISTENERS.pop(ctrl_id, None)
            cell_link_registry.clear(control)

        def statusChanged(self, ev: Any) -> None:
            try:
                self.caret_addr = cell_address_from_font_state(ev.State)
                if self.pending_until and time.monotonic() <= self.pending_until:
                    self.pending_until = 0.0
                    if self.caret_addr:
                        _navigate(self.caret_addr)
            except Exception:
                log.exception("cell link status handler failed")

        def mousePressed(self, e: Any) -> None:
            self.press = (int(e.X), int(e.Y))
            self.pending_until = 0.0

        def mouseReleased(self, e: Any) -> None:
            try:
                if e.Buttons != _MB_LEFT or e.ClickCount != 1:
                    return
                if e.Modifiers:
                    return
                x, y = int(e.X), int(e.Y)
                press, self.press = self.press, None
                if press is not None and (abs(press[0] - x) > _CLICK_SLOP_PX or abs(press[1] - y) > _CLICK_SLOP_PX):
                    return
                addr = self.caret_addr
                if not addr:
                    idx = _click_text_index(control, x, y)
                    if idx is not None:
                        addr = lookup_cell_ref_at_index(control, idx)
                if not addr:
                    # The button-up attribute refresh may still report the link.
                    self.pending_until = time.monotonic() + _PENDING_CLICK_SEC
                    return
                _navigate(addr)
            except Exception:
                log.exception("cell link click handler failed")

    listener = _CalcCellLinkMouseListener()
    control.addMouseListener(listener)
    listener.status_dispatch = best_effort(
        lambda: _add_caret_font_status_listener(control, listener), "cell link: caret status listener"
    )
    _CELL_LINK_LISTENERS[ctrl_id] = listener
    log.debug(
        "attached calc cell link mouse listener control=%s caret_status=%s", ctrl_id, listener.status_dispatch is not None
    )
