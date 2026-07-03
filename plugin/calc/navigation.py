# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc cell navigation helpers and sidebar ``cell://`` link rendering."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Callable

import uno

from plugin.calc.address_utils import parse_address

log = logging.getLogger("writeragent.calc")

_XACCESSIBLE_TEXT = "com.sun.star.accessibility.XAccessibleText"


def _query_interface(obj: Any, typename: str) -> Any:
    """PyUNO requires ``uno.getTypeByName`` for ``queryInterface``; imported IDL classes fail."""

    return obj.queryInterface(uno.getTypeByName(typename))

# HTML: <a href="cell://B2">B2</a> (Calc chat uses HTML, not markdown)
_CELL_LINK_HTML_RE = re.compile(
    r'(href\s*=\s*)(["\'])cell://([^"\']+)\2',
    re.IGNORECASE,
)
_CELL_HREF_RE = re.compile(
    r"""href\s*=\s*(["'])(?:cell://|writeragent-cell://)([^"']+)\1""",
    re.IGNORECASE,
)
_CELL_LINK_ANCHOR_RE = re.compile(
    r"""<a\s+[^>]*href\s*=\s*(["'])(?:cell://|writeragent-cell://)([^"']+)\1[^>]*>(.*?)</a>""",
    re.IGNORECASE | re.DOTALL,
)
# Plain cell address (optional sheet prefix): Sheet1.B2 or B2
_CELL_ADDRESS_RE = re.compile(
    r"^(?:([A-Za-z0-9_]+)\.)?([A-Z]{1,3})(\d{1,7})$",
    re.IGNORECASE,
)
WRITERAGENT_CELL_URL_PREFIX = "writeragent-cell://"

_CELL_LINK_LISTENERS: dict[int, Any] = {}


@dataclass
class CellLinkSpanRegistry:
    """Maps sidebar RichTextControl character offsets to Calc cell addresses."""

    _spans: dict[int, list[tuple[int, int, str]]] = field(default_factory=dict)

    def clear(self, control) -> None:
        if control is not None:
            self._spans.pop(id(control), None)

    def add(self, control, start: int, end: int, address: str) -> None:
        if control is None or start >= end or not address:
            return
        self._spans.setdefault(id(control), []).append((start, end, address))

    def lookup(self, control, index: int) -> str | None:
        if control is None or index < 0:
            return None
        for start, end, addr in self._spans.get(id(control), []):
            if start <= index < end:
                return addr
        return None


cell_link_registry = CellLinkSpanRegistry()


def normalize_cell_address(raw: str) -> str | None:
    """Return a canonical cell address (``Sheet1.B2`` or ``B2``) or *None*."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.lower().startswith(WRITERAGENT_CELL_URL_PREFIX):
        text = text[len(WRITERAGENT_CELL_URL_PREFIX) :]
    if text.lower().startswith("cell://"):
        text = text[7:]
    match = _CELL_ADDRESS_RE.match(text.strip())
    if not match:
        return None
    sheet, col, row = match.group(1), match.group(2), match.group(3)
    addr = f"{col.upper()}{row}"
    if sheet:
        return f"{sheet}.{addr}"
    return addr


def extract_cell_links_from_html(html: str) -> list[tuple[str, str]]:
    """Return ``(label, address)`` pairs from HTML ``cell://`` anchors in document order."""
    if not html or "cell://" not in html.lower() and WRITERAGENT_CELL_URL_PREFIX not in html.lower():
        return []
    links: list[tuple[str, str]] = []
    for match in _CELL_LINK_ANCHOR_RE.finditer(html):
        addr = normalize_cell_address(match.group(2))
        label = re.sub(r"<[^>]+>", "", match.group(3))
        label = unescape(label).strip()
        if addr and label:
            links.append((label, addr))
    return links


def render_calc_cell_refs(text: str) -> str:
    """Rewrite ``cell://`` in HTML ``<a href>`` to the internal sidebar link scheme."""
    if not text or "cell://" not in text.lower():
        return text

    def _rewrite_href(match: re.Match[str]) -> str:
        prefix, quote, raw_addr = match.group(1), match.group(2), match.group(3)
        addr = normalize_cell_address(raw_addr)
        if not addr:
            return match.group(0)
        return f"{prefix}{quote}{WRITERAGENT_CELL_URL_PREFIX}{addr}{quote}"

    return _CELL_LINK_HTML_RE.sub(_rewrite_href, text)


def portion_cell_href(portion) -> str | None:
    """Read a ``cell://`` or ``writeragent-cell://`` URL from a Writer text portion, if any."""
    if portion is None:
        return None
    for prop in ("HyperLinkURL", "HyperlinkURL", "HYPERLINK"):
        try:
            url = getattr(portion, prop, None)
        except Exception:
            url = None
        if isinstance(url, str) and url and ("cell://" in url.lower() or WRITERAGENT_CELL_URL_PREFIX in url.lower()):
            return url
    try:
        url = portion.getPropertyValue("HyperLinkURL")
        if isinstance(url, str) and url and ("cell://" in url.lower() or WRITERAGENT_CELL_URL_PREFIX in url.lower()):
            return url
    except Exception:
        pass
    return None


def portion_looks_like_cell_link(portion, text: str) -> bool:
    """True when *text* is a cell address and the portion is underlined like a hyperlink."""
    if not text or normalize_cell_address(text.strip()) is None:
        return False
    try:
        underline = int(getattr(portion, "CharUnderline", 0) or 0)
        return underline != 0
    except (TypeError, ValueError):
        return False


def register_cell_link_span(control, start: int, end: int, address: str) -> None:
    cell_link_registry.add(control, start, end, address)


def clear_cell_link_spans(control) -> None:
    cell_link_registry.clear(control)


def resolve_sheet_and_cell(doc, address: str) -> tuple[Any, int, int] | None:
    """Resolve *address* to ``(sheet, col, row)`` for the open Calc document."""
    target = normalize_cell_address(address)
    if not target or doc is None:
        return None
    sheet_name = None
    cell_part = target
    if "." in target:
        sheet_name, cell_part = target.rsplit(".", 1)
        try:
            parse_address(cell_part)
        except ValueError:
            sheet_name = None
            cell_part = target
    try:
        col, row = parse_address(cell_part)
    except ValueError:
        return None

    controller = doc.getCurrentController()
    if sheet_name:
        sheets = doc.getSheets()
        if not sheets.hasByName(sheet_name):
            return None
        sheet = sheets.getByName(sheet_name)
    elif controller is not None:
        sheet = controller.getActiveSheet()
    else:
        sheets = doc.getSheets()
        sheet = sheets.getByIndex(0) if sheets.getCount() else None
    if sheet is None:
        return None
    return sheet, col, row


def navigate_to_cell(doc, ctx, address: str) -> bool:
    """Select *address* in the Calc document (PyUNO select, not sidebar dispatch)."""
    unused_ctx = ctx  # kept for callers; navigation uses the document controller directly
    resolved = resolve_sheet_and_cell(doc, address)
    if not resolved:
        log.debug("navigate_to_cell: could not resolve %r", address)
        return False
    sheet, col, row = resolved
    try:
        controller = doc.getCurrentController()
        if controller is None:
            return False
        if controller.getActiveSheet().getName() != sheet.getName():
            controller.setActiveSheet(sheet)
        cell = sheet.getCellByPosition(col, row)
        controller.select(cell)
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


def lookup_cell_ref_at_index(control, index: int) -> str | None:
    """Resolve a cell address at *index* using the span registry, then plain text."""
    addr = cell_link_registry.lookup(control, index)
    if addr:
        return addr
    if control is None or not hasattr(control, "getText"):
        return None
    text = control.getText()
    if not isinstance(text, str):
        return None
    return cell_ref_at_index(text, index)


def _accessible_text(control) -> Any | None:
    try:
        ctx = control.getAccessibleContext()
        if ctx is None:
            return None
        ax = _query_interface(ctx, _XACCESSIBLE_TEXT)
        if ax:
            return ax
        for i in range(ctx.getAccessibleChildCount()):
            child = ctx.getAccessibleChild(i)
            if child is None:
                continue
            child_ctx = child.getAccessibleContext() if hasattr(child, "getAccessibleContext") else child
            if child_ctx is not None:
                ax = _query_interface(child_ctx, _XACCESSIBLE_TEXT)
                if ax:
                    return ax
    except Exception:
        log.debug("cell link: accessible text lookup failed", exc_info=True)
    return None


def _char_index_at_point(control, x: int, y: int) -> int | None:
    axtext = _accessible_text(control)
    if axtext is None:
        return None
    try:
        from com.sun.star.awt import Point

        idx = axtext.getIndexAtPoint(Point(x, y))
        if isinstance(idx, int) and idx >= 0:
            return idx
    except Exception:
        log.debug("cell link: getIndexAtPoint failed", exc_info=True)
    return None


def _click_text_index(control, x: int, y: int) -> int | None:
    """Character index under the mouse, preferring the control caret after click."""
    model = control.getModel() if control is not None and hasattr(control, "getModel") else None
    if model is not None:
        for prop in ("SelectionStart", "Selection"):
            try:
                idx = getattr(model, prop, None)
                if isinstance(idx, int) and idx >= 0:
                    return idx
            except Exception:
                pass
    return _char_index_at_point(control, x, y)


def attach_calc_cell_link_listener(ctx, control, get_calc_doc: Callable[[], Any | None]) -> None:
    """Attach a mouse listener on *control* for ``cell://`` navigation."""
    if control is None or ctx is None:
        return
    ctrl_id = id(control)
    if ctrl_id in _CELL_LINK_LISTENERS:
        return
    try:
        import unohelper
        from com.sun.star.awt import XMouseListener
    except ImportError:
        log.debug("cell link listener skipped: PyUNO unavailable")
        return

    _MB_LEFT = 1  # com.sun.star.awt.MouseButton.LEFT

    class _CalcCellLinkMouseListener(unohelper.Base, XMouseListener):  # type: ignore[misc]
        def disposing(self, Source) -> None:
            _CELL_LINK_LISTENERS.pop(ctrl_id, None)

        def mousePressed(self, e) -> None:
            pass

        def mouseReleased(self, e) -> None:
            try:
                if getattr(e, "Buttons", 0) != _MB_LEFT or getattr(e, "ClickCount", 0) != 1:
                    return
                idx = _click_text_index(control, int(e.X), int(e.Y))
                if idx is None:
                    return
                addr = lookup_cell_ref_at_index(control, idx)
                if not addr:
                    return
                doc = get_calc_doc()
                if doc is None:
                    log.debug("cell link click: no Calc document for %r", addr)
                    return
                if navigate_to_cell(doc, ctx, addr):
                    log.debug("cell link navigated to %r", addr)
            except Exception:
                log.exception("cell link click handler failed")

        def mouseEntered(self, e) -> None:
            pass

        def mouseExited(self, e) -> None:
            pass

    listener = _CalcCellLinkMouseListener()
    control.addMouseListener(listener)
    _CELL_LINK_LISTENERS[ctrl_id] = listener
    log.debug("attached calc cell link mouse listener control=%s", ctrl_id)
