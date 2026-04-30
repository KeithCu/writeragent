# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Insert LibreOffice Math (OLE) on Draw/Impress pages."""

from __future__ import annotations

import logging
from typing import Any

from plugin.modules.draw.base import ToolDrawSpecialBase

log = logging.getLogger("writeragent.draw")

MATH_CLSID = "078B7ABA-54FC-457F-8551-6147e776a997"

# Fallback when UNO does not report a usable visual size (100ths of mm).
_FALLBACK_WIDTH_HMM = 5000
_FALLBACK_HEIGHT_HMM = 2000
_MIN_W_HMM, _MIN_H_HMM = 500, 400
_MAX_W_HMM, _MAX_H_HMM = 120_000, 80_000


def _visual_size_to_hundredth_mm(sz: Any, map_unit: int) -> tuple[int, int] | None:
    """Convert embed visual Size + EmbedMapUnits to shape units (1/100 mm)."""
    try:
        from com.sun.star.embed import EmbedMapUnits

        w, h = int(sz.Width), int(sz.Height)
        if w <= 0 or h <= 0:
            return None

        mu = int(map_unit)
        if mu == int(EmbedMapUnits.ONE_100TH_MM):
            return w, h
        if mu == int(EmbedMapUnits.TWIP):
            return round(w * 2540 / 1440), round(h * 2540 / 1440)
        if mu == int(EmbedMapUnits.POINT):
            return round(w * 2540 / 72), round(h * 2540 / 72)
        if mu == int(EmbedMapUnits.ONE_MM):
            return w * 100, h * 100
        if mu == int(EmbedMapUnits.ONE_100TH_INCH):
            return round(w * 254 / 10), round(h * 254 / 10)
        if mu == int(EmbedMapUnits.ONE_10TH_MM):
            return w * 10, h * 10
        if mu == int(EmbedMapUnits.ONE_CM):
            return w * 1000, h * 1000
    except Exception:
        log.debug("math_insert: map unit conversion failed", exc_info=True)
    return None


def _try_ole_shape_content_size_hmm(shape: Any) -> tuple[int, int] | None:
    """Best-effort size from embedded object's XVisualObject.getVisualAreaSize."""
    try:
        from com.sun.star.embed import Aspects
        import uno

        emb = None
        if hasattr(shape, "getEmbeddedObject"):
            try:
                emb = shape.getEmbeddedObject()
            except Exception:
                emb = None
        if emb is None:
            return None
        vo = emb.queryInterface(uno.getTypeByName("com.sun.star.embed.XVisualObject"))
        if vo is None:
            return None
        aspect = int(getattr(Aspects, "MSOLE_CONTENT", 1))
        sz = vo.getVisualAreaSize(aspect)
        map_unit = vo.getMapUnit(aspect)
        out = _visual_size_to_hundredth_mm(sz, map_unit)
        if out is None:
            log.debug(
                "math_insert: unsupported embed map_unit=%s size=%sx%s",
                map_unit,
                getattr(sz, "Width", "?"),
                getattr(sz, "Height", "?"),
            )
        return out
    except Exception:
        log.debug("math_insert: getVisualAreaSize path failed", exc_info=True)
    return None


def _uno_ctx_from_tool_ctx(ctx: Any) -> Any:
    """``ToolContext`` carries the UNO component context in ``.ctx``; plain UNO ctx passes through."""
    u = getattr(ctx, "ctx", None)
    return u if u is not None else ctx


def _heuristic_size_hmm(formula: str) -> tuple[int, int]:
    """Rough box from formula length when UNO sizing is unavailable."""
    n = max(8, len(formula.strip()))
    # ~550 hmm per 10 chars width, height grows slowly with line breaks
    lines = max(1, formula.count("newline") + formula.count("\n") + 1)
    w = min(_MAX_W_HMM, max(_MIN_W_HMM, n * 55))
    h = min(_MAX_H_HMM, max(_MIN_H_HMM, 800 + lines * 450))
    return w, h


class InsertMathDraw(ToolDrawSpecialBase):
    name = "insert_math"
    intent = "insert"
    description = (
        "Inserts an editable LibreOffice Math formula on a Draw or Impress page. "
        "Use formula_type 'latex' or 'mathml' with the corresponding formula string; "
        "position with page_index, x, y (100ths of mm). Size is derived from the formula when possible."
    )
    parameters = {
        "type": "object",
        "properties": {
            "formula_type": {
                "type": "string",
                "enum": ["latex", "mathml"],
                "description": "Whether formula is LaTeX (converted via LO) or MathML.",
            },
            "formula": {
                "type": "string",
                "description": "LaTeX or MathML formula text, per formula_type.",
            },
            "page_index": {
                "type": "integer",
                "description": "Zero-based index of the draw page or slide.",
            },
            "x": {
                "type": "integer",
                "description": "X position of the shape (100ths of mm).",
            },
            "y": {
                "type": "integer",
                "description": "Y position of the shape (100ths of mm).",
            },
        },
        "required": ["formula_type", "formula", "page_index", "x", "y"],
    }
    uno_services = [
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument",
    ]
    is_mutation = True
    specialized_domain = "math"

    def execute(self, ctx: Any, **kwargs: Any) -> Any:
        from plugin.modules.writer.math_mml_convert import convert_latex_to_starmath, convert_mathml_to_starmath
        from plugin.modules.draw.bridge import DrawBridge
        from com.sun.star.awt import Size, Point

        formula_type = kwargs.get("formula_type")
        formula = kwargs.get("formula")
        if not isinstance(formula, str) or not formula.strip():
            return self._tool_error("formula must be a non-empty string.")

        uno_ctx = _uno_ctx_from_tool_ctx(ctx)

        if formula_type == "latex":
            fstrip = formula.strip()
            display_block = (
                "\\displaystyle" in fstrip.lower()
                or "\\int" in fstrip
                or "\\iint" in fstrip
                or "\\iiint" in fstrip
                or "\\oint" in fstrip
            )
            res = convert_latex_to_starmath(
                uno_ctx, fstrip, display_block=display_block
            )
        elif formula_type == "mathml":
            res = convert_mathml_to_starmath(uno_ctx, formula.strip())
        else:
            return self._tool_error("formula_type must be 'latex' or 'mathml'.")

        if not res.ok:
            detail = (res.error_message or "").strip() or "unknown_error"
            return self._tool_error(
                f"Failed to convert math expression: {detail}",
                conversion_detail=detail,
            )

        try:
            page_index = int(kwargs["page_index"])
            x = int(kwargs["x"])
            y = int(kwargs["y"])
        except (KeyError, TypeError, ValueError):
            return self._tool_error("page_index, x, and y must be integers.")

        bridge = DrawBridge(ctx.doc)
        pages = bridge.get_pages()
        if page_index < 0 or page_index >= pages.getCount():
            return self._tool_error("Invalid page_index.")
        page = pages.getByIndex(page_index)

        try:
            shape = ctx.doc.createInstance("com.sun.star.drawing.OLE2Shape")
            # Draw/Impress OLE: add to page first, then CLSID (see charts OLE path).
            page.add(shape)
            shape.setPosition(Point(x, y))
            shape.setSize(Size(_MIN_W_HMM, _MIN_H_HMM))
            shape.CLSID = MATH_CLSID

            model = shape.Model
            if model is None or not hasattr(model, "Formula"):
                return self._tool_error("Math OLE model is not available on this build.")
            model.Formula = res.starmath

            wh = _try_ole_shape_content_size_hmm(shape)
            if wh is None or wh[0] < _MIN_W_HMM or wh[1] < _MIN_H_HMM:
                wh = _heuristic_size_hmm(res.starmath or formula)
            w, h = wh
            w = max(_MIN_W_HMM, min(_MAX_W_HMM, w))
            h = max(_MIN_H_HMM, min(_MAX_H_HMM, h))
            shape.setSize(Size(w, h))

        except Exception as e:
            return self._tool_error(f"Failed to insert math shape: {e}")

        return {
            "status": "ok",
            "message": "Math formula inserted successfully",
            "shape_index": page.getCount() - 1,
            "page_index": page_index,
        }
