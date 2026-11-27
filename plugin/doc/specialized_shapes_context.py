# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shapes-domain delegation helpers: canvas facts in, outcome summary out."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from plugin.framework.errors import uno_read
from plugin.framework.thread_guard import assert_main_thread

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TypeGuard

    from plugin.doc.specialized_catalog import InnerToolCall


def _writer_page_style_name_at_cursor(doc: Any) -> str | None:
    """Best-effort page style name from the paragraph at the view cursor."""
    def _body() -> str | None:
        ctrl = doc.getCurrentController()
        vc = ctrl.getViewCursor()
        # vc.getText() is the cursor's XText (header, frame, cell)—doc.getText() is body-only.
        cur = vc.getText().createTextCursorByRange(vc.getStart())
        cur.gotoStartOfParagraph(False)
        psi = cur.getPropertySetInfo()
        for prop in ("PageDescName", "PageStyleName"):
            if not psi.hasPropertyByName(prop):
                continue
            v = cur.getPropertyValue(prop)
            if v is None:
                continue
            s = str(v).strip()
            if s:
                return s
        return None

    return uno_read(_body, None)


def _writer_canvas_for_style(doc: Any, style_name: str) -> str | None:
    def _body() -> str | None:
        page_styles = doc.getStyleFamilies().getByName("PageStyles")
        if not page_styles.hasByName(style_name):
            return None
        style = page_styles.getByName(style_name)
        w = int(style.getPropertyValue("Width"))
        h = int(style.getPropertyValue("Height"))
        lm = int(style.getPropertyValue("LeftMargin"))
        rm = int(style.getPropertyValue("RightMargin"))
        tm = int(style.getPropertyValue("TopMargin"))
        bm = int(style.getPropertyValue("BottomMargin"))
        orient = "landscape" if bool(style.getPropertyValue("IsLandscape")) else "portrait"
        return (
            " Document canvas (Writer): "
            f"page style '{style_name}'; paper {w} x {h} ({orient}); "
            f"margins L/R/T/B {lm}/{rm}/{tm}/{bm} (1/100 mm). "
            "For page-anchored shapes, (0,0) is relative to the printable page area (inside margins), not the paper edge."
        )

    return uno_read(_body, None)


def _format_writer_canvas(doc: Any) -> str:
    resolved = _writer_page_style_name_at_cursor(doc)
    for name in dict.fromkeys(n for n in (resolved, "Standard", "Default Page Style") if n):
        text = _writer_canvas_for_style(doc, name)
        if text:
            return text
    return ""


def _format_draw_canvas(doc: Any, *, product_label: str) -> str:
    def _body() -> str:
        # Lazy: cross-app — the Draw/Calc bridge is only loaded when that app's document is the target.
        from plugin.draw.bridge import DrawBridge, page_size

        bridge = DrawBridge(doc)
        pages = bridge.get_pages()
        n_pages = pages.getCount()
        page = bridge.get_active_page()
        if page is None:
            return ""
        idx = bridge.get_active_page_index()
        size = page_size(page)
        if size is None:
            return ""
        w, h = size
        return f" Document canvas ({product_label}): active slide/page index {idx} (0-based: the user's page {idx + 1}), size {w} x {h} (1/100 mm); {n_pages} page(s) total."

    return uno_read(_body, "")


def _format_calc_canvas(doc: Any) -> str:
    """Active sheet draw page size (floating objects / charts on sheet)."""
    def _body() -> str:
        # Lazy: cross-app — the Draw/Calc bridge is only loaded when that app's document is the target.
        from plugin.calc.bridge import CalcBridge
        from plugin.draw.bridge import page_size

        bridge = CalcBridge(doc)
        sheet = bridge.get_active_sheet()
        dp = sheet.getDrawPage()
        size = page_size(dp)
        size_part = f"draw-page size {size[0]} x {size[1]} (1/100 mm)" if size else "no fixed page size"

        n_sheets = doc.getSheets().getCount()
        sheet_idx = sheet.getRangeAddress().Sheet
        sheet_name = sheet.getName().strip()
        name_part = f", name '{sheet_name}'" if sheet_name else ""

        return f" Document canvas (Calc): active sheet index {sheet_idx} (0-based){name_part}, {size_part}; {n_sheets} sheet(s)."

    return uno_read(_body, "")


_SHAPES_CANVAS_DISPATCH = (
    ("com.sun.star.text.TextDocument", _format_writer_canvas),
    ("com.sun.star.sheet.SpreadsheetDocument", _format_calc_canvas),
    ("com.sun.star.presentation.PresentationDocument", lambda doc: _format_draw_canvas(doc, product_label="Impress")),
    ("com.sun.star.drawing.DrawingDocument", lambda doc: _format_draw_canvas(doc, product_label="Draw")),
)


def format_shapes_canvas_context(doc: Any) -> str:
    """Human-readable canvas line for shapes specialized sub-agent instructions."""
    assert_main_thread("specialized_shapes_context.format_shapes_canvas_context")
    if doc is None:
        return ""
    for service, handler in _SHAPES_CANVAS_DISPATCH:
        if doc.supportsService(service):
            return handler(doc)
    return ""


# Matches _lookup_ref's available-names cap in plugin/draw/shapes.py.
_SHAPE_NAME_CAP = 40


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _record_name(target: list[str], raw_name: Any) -> None:
    if not isinstance(raw_name, str):
        return
    name = raw_name.strip()
    if not name or name in target or len(target) >= _SHAPE_NAME_CAP:
        return
    target.append(name)


def _add_warning(warnings: list[Any], item: Any) -> None:
    # Diagnostic is a TypedDict (unhashable), so check membership instead of dict.fromkeys.
    if item and item not in warnings:
        warnings.append(item)


def _failed_field_label(entry: dict[str, Any], pos: int) -> str:
    for key in ("name", "label_hint"):
        val = entry.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for key in ("index", "path_index"):
        val = entry.get(key)
        if val is not None:
            return str(val)
    return f"field {pos}"


def summarize_shape_calls(calls: Sequence[InnerToolCall]) -> dict[str, Any]:
    """What a shapes delegation placed or changed, read from its own inner tool results.

    Counts lead because LibreOffice leaves API-created shapes unnamed unless
    ``name=`` is passed (``svx/source/unodraw/unoshape.cxx:SvxShape::getName``).
    Indices are omitted because a later ``shape_group`` or ``shape_delete`` in
    the same run shifts page indices. Only gateway ``shapes`` delegations use
    this helper; the Python ``python_delegate_domains`` path creates shapes inside
    scripts that never enter ``InnerToolCall`` logs.
    """
    shapes_created = 0
    connectors_created = 0
    shapes_changed = 0
    shapes_deleted = 0
    pages_set: set[int] = set()
    created_names: list[str] = []
    changed_names: list[str] = []
    warnings: list[Any] = []

    for call in calls:
        if not call.mutating:
            continue
        res = call.result
        if not isinstance(res, dict):
            continue
        if res.get("status") not in ("ok", "partial"):
            continue

        page = res.get("page")
        if _is_int(page):
            pages_set.add(page)

        for w in res.get("warnings") or ():
            _add_warning(warnings, w)
        _add_warning(warnings, res.get("warning"))

        name = call.name
        if name == "shape_upsert" and res.get("action") == "create":
            shapes_created += 1
            _record_name(created_names, res.get("name"))
        elif name == "shape_connect":
            connectors_created += 1
        elif name == "shape_create_diagram":
            shapes_created += len(res.get("nodes", []))
            connectors_created += len(res.get("connections", []))
        elif name == "shape_fill_fields":
            shapes_changed += int(res.get("ok_count", 0))
            failed_labels: list[str] = []
            for pos, entry in enumerate(res.get("results", ())):
                if entry.get("ok"):
                    _record_name(changed_names, entry.get("name"))
                else:
                    failed_labels.append(_failed_field_label(entry, pos))
            fail_count = int(res.get("fail_count", 0))
            if fail_count > 0:
                suffix = f": {', '.join(failed_labels)}" if failed_labels else ""
                _add_warning(warnings, f"shape_fill_fields left {fail_count} field(s) unfilled{suffix}")
        elif name == "shape_delete":
            shapes_deleted += 1
        else:
            shapes_changed += len(res.get("indices") or ()) or 1
            _record_name(changed_names, res.get("name"))

    out: dict[str, Any] = {
        "shapes_created": shapes_created,
        "connectors_created": connectors_created,
        "shapes_changed": shapes_changed,
        "shapes_deleted": shapes_deleted,
    }
    if pages_set:
        out["pages"] = sorted(pages_set)
    if created_names:
        out["created_names"] = created_names
    if changed_names:
        out["changed_names"] = changed_names
    if warnings:
        out["warnings"] = warnings
    return out
