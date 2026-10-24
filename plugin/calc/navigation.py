# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc cell navigation helpers."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable


from plugin.calc.calc_utils import query_interface as _query_interface, resolve_sheet_and_cell as _canonical_resolve_sheet_and_cell
from plugin.framework.errors import best_effort
from plugin.framework.uno_listeners import BaseMouseListener

log = logging.getLogger("writeragent.calc")

_XACCESSIBLE_TEXT = "com.sun.star.accessibility.XAccessibleText"

_CELL_HREF_RE = re.compile(r"""href\s*=\s*(["'])(?:cell://|writeragent-cell://)([^"']+)\1""", re.IGNORECASE)
# Plain cell address (optional sheet prefix): Sheet1.B2, Excel Sheet1!B2, or B2.
# Canonical form is always the Calc dot (Sheet1.B2).
_CELL_ADDRESS_RE = re.compile(r"^(?:(?:'([^']+)'|([^.!]+))[.!])?([A-Z]{1,3})(\d{1,7})$", re.IGNORECASE)
WRITERAGENT_CELL_URL_PREFIX = "writeragent-cell://"

_CELL_LINK_LISTENERS: dict[int, Any] = {}


@dataclass
class CellLinkSpanRegistry:
    """Maps sidebar RichTextControl character offsets to Calc cell addresses."""

    _spans: dict[int, list[tuple[int, int, str]]] = field(default_factory=dict)

    def clear(self, control: Any) -> None:
        if control is not None:
            self._spans.pop(id(control), None)

    def add(self, control: Any, start: int, end: int, address: str) -> None:
        if control is None or start >= end or not address:
            return
        self._spans.setdefault(id(control), []).append((start, end, address))

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
        if control is None:
            return
        key = id(control)
        spans = self._spans.get(key)
        if not spans:
            return
        kept = [span for span in spans if span[1] <= start_len]
        if kept:
            self._spans[key] = kept
        else:
            self._spans.pop(key, None)


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
    sheet = match.group(1) or match.group(2)
    col = match.group(3)
    row = match.group(4)
    addr = f"{col.upper()}{row}"
    if sheet:
        return f"{sheet}.{addr}"
    return addr


def register_cell_link_span(control: Any, start: int, end: int, address: str) -> None:
    cell_link_registry.add(control, start, end, address)


def clear_cell_link_spans(control: Any) -> None:
    cell_link_registry.clear(control)


def drop_cell_link_spans_from(control: Any, start_len: int) -> None:
    cell_link_registry.drop_from(control, start_len)


def resolve_sheet_and_cell(doc: Any, address: str) -> tuple[Any, int, int] | None:
    """Resolve *address* to ``(sheet, col, row)`` for the open Calc document."""
    target = normalize_cell_address(address)
    if not target or doc is None:
        return None
    return _canonical_resolve_sheet_and_cell(doc, target)


def navigate_to_cell(doc: Any, _ctx: Any, address: str) -> bool:
    """Select *address* in the Calc document (PyUNO select, not sidebar dispatch)."""
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


def lookup_cell_ref_at_index(control: Any, index: int) -> str | None:
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


def _accessible_text(control: Any) -> Any | None:
    def _lookup() -> Any | None:
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
    model = control.getModel() if control is not None and hasattr(control, "getModel") else None
    if model is not None:
        for prop in ("SelectionStart", "Selection"):
            idx = getattr(model, prop, None)
            if isinstance(idx, int) and idx >= 0:
                return idx
    return _char_index_at_point(control, x, y)


def attach_calc_cell_link_listener(ctx: Any, control: Any, get_calc_doc: Callable[[], Any | None]) -> None:
    """Attach a mouse listener on *control* for ``cell://`` navigation."""
    if control is None or ctx is None:
        return
    ctrl_id = id(control)
    if ctrl_id in _CELL_LINK_LISTENERS:
        return
    _MB_LEFT = 1  # com.sun.star.awt.MouseButton.LEFT

    class _CalcCellLinkMouseListener(BaseMouseListener):
        def disposing(self, Source: Any) -> None:
            _CELL_LINK_LISTENERS.pop(ctrl_id, None)
            cell_link_registry.clear(control)

        def mouseReleased(self, e: Any) -> None:
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

    listener = _CalcCellLinkMouseListener()
    control.addMouseListener(listener)
    _CELL_LINK_LISTENERS[ctrl_id] = listener
    log.debug("attached calc cell link mouse listener control=%s", ctrl_id)
