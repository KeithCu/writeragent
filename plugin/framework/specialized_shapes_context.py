# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Page/slide canvas facts for specialized shapes sub-agents (delegation only)."""

import logging
from typing import Any

log = logging.getLogger("writeragent.specialized_shapes_context")


def _writer_page_style_name_at_cursor(doc: Any) -> str | None:
    """Best-effort page style name from the paragraph at the view cursor."""
    try:
        ctrl = doc.getCurrentController()
        if ctrl is None:
            return None
        vc = ctrl.getViewCursor()
        text = doc.getText()
        cur = text.createTextCursorByRange(vc.getStart())
        cur.gotoStartOfParagraph(False)
        psi = cur.getPropertySetInfo()
        if psi is None:
            return None
        for prop in ("PageDescName", "PageStyleName"):
            if not psi.hasPropertyByName(prop):
                continue
            try:
                v = cur.getPropertyValue(prop)
            except Exception:
                continue
            if v is None:
                continue
            s = str(v).strip()
            if s:
                return s
    except Exception:
        log.debug("writer_page_style_at_cursor: failed", exc_info=True)
    return None


def _writer_fallback_style_names() -> tuple[str, ...]:
    return ("Standard", "Default Page Style")


def _read_writer_page_style_mm(doc: Any, style_name: str) -> dict[str, Any] | None:
    try:
        page_styles = doc.getStyleFamilies().getByName("PageStyles")
        if not page_styles.hasByName(style_name):
            return None
        style = page_styles.getByName(style_name)
        w = float(style.getPropertyValue("Width")) / 100.0
        h = float(style.getPropertyValue("Height")) / 100.0
        lm = float(style.getPropertyValue("LeftMargin")) / 100.0
        rm = float(style.getPropertyValue("RightMargin")) / 100.0
        tm = float(style.getPropertyValue("TopMargin")) / 100.0
        bm = float(style.getPropertyValue("BottomMargin")) / 100.0
        landscape = bool(style.getPropertyValue("IsLandscape"))
        return {"style_name": style_name, "width_mm": w, "height_mm": h, "left_margin_mm": lm, "right_margin_mm": rm, "top_margin_mm": tm, "bottom_margin_mm": bm, "is_landscape": landscape}
    except Exception:
        log.debug("read_writer_page_style_mm: failed for %r", style_name, exc_info=True)
        return None


def _format_writer_canvas(doc: Any) -> str:
    resolved = _writer_page_style_name_at_cursor(doc)
    candidates: list[str] = []
    if resolved:
        candidates.append(resolved)
    candidates.extend(n for n in _writer_fallback_style_names() if n not in candidates)

    geom: dict[str, Any] | None = None
    used_style = ""
    for name in candidates:
        geom = _read_writer_page_style_mm(doc, name)
        if geom:
            used_style = name
            break

    if not geom:
        return ""

    orient = "landscape" if geom["is_landscape"] else "portrait"
    return (
        " Document canvas (Writer): "
        f"page style '{used_style}'; paper {geom['width_mm']:.1f} x {geom['height_mm']:.1f} mm ({orient}); "
        f"margins L/R/T/B {geom['left_margin_mm']:.1f}/{geom['right_margin_mm']:.1f}/"
        f"{geom['top_margin_mm']:.1f}/{geom['bottom_margin_mm']:.1f} mm. "
        "Shape x/y/width/height use LibreOffice units of 1/100 mm. "
        "For page-anchored shapes, (0,0) is relative to the printable page area (inside margins), not the paper edge."
    )


def _format_draw_canvas(doc: Any, *, product_label: str) -> str:
    try:
        from plugin.modules.draw.bridge import DrawBridge

        bridge = DrawBridge(doc)
        pages = bridge.get_pages()
        n_pages = int(pages.getCount())
        page = bridge.get_active_page()
        if page is None:
            return ""
        idx = int(bridge.get_active_page_index())
        w_mm = float(getattr(page, "Width", 0) or 0) / 100.0
        h_mm = float(getattr(page, "Height", 0) or 0) / 100.0
        if w_mm <= 0 or h_mm <= 0:
            return ""
        return f" Document canvas ({product_label}): active slide/page index {idx} (0-based), size {w_mm:.1f} x {h_mm:.1f} mm; {n_pages} page(s) total. Shape x/y/width/height use 1/100 mm."
    except Exception:
        log.debug("format_draw_canvas: failed", exc_info=True)
        return ""


def _format_calc_canvas(doc: Any) -> str:
    """Active sheet draw page size (floating objects / charts on sheet)."""
    try:
        from plugin.modules.calc.bridge import CalcBridge

        bridge = CalcBridge(doc)
        sheet = bridge.get_active_sheet()
        dp = sheet.getDrawPage()
        w_mm = float(getattr(dp, "Width", 0) or 0) / 100.0
        h_mm = float(getattr(dp, "Height", 0) or 0) / 100.0
        if w_mm <= 0 or h_mm <= 0:
            return ""

        sheets = doc.getSheets()
        n_sheets = int(sheets.getCount())
        sheet_idx = 0
        for i in range(n_sheets):
            try:
                if sheets.getByIndex(i) is sheet:
                    sheet_idx = i
                    break
            except Exception:
                pass
            try:
                if sheets.getByIndex(i) == sheet:
                    sheet_idx = i
                    break
            except Exception:
                pass

        sheet_name = ""
        try:
            sheet_name = str(getattr(sheet, "Name", "") or "").strip()
        except Exception:
            pass
        name_part = f", name '{sheet_name}'" if sheet_name else ""

        return f" Document canvas (Calc): active sheet index {sheet_idx} (0-based){name_part}, draw-page size {w_mm:.1f} x {h_mm:.1f} mm; {n_sheets} sheet(s). Shape x/y/width/height use 1/100 mm."
    except Exception:
        log.debug("format_calc_canvas: failed", exc_info=True)
        return ""


def format_shapes_canvas_context(doc: Any) -> str:
    """Human-readable canvas line for shapes specialized sub-agent instructions."""
    if doc is None:
        return ""
    try:
        if doc.supportsService("com.sun.star.text.TextDocument"):
            return _format_writer_canvas(doc)
        if doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
            return _format_calc_canvas(doc)
        if doc.supportsService("com.sun.star.presentation.PresentationDocument"):
            return _format_draw_canvas(doc, product_label="Impress")
        if doc.supportsService("com.sun.star.drawing.DrawingDocument"):
            return _format_draw_canvas(doc, product_label="Draw")
    except Exception:
        log.debug("format_shapes_canvas_context: failed", exc_info=True)
    return ""
