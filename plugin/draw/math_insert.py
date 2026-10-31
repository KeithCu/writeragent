# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Insert LibreOffice Math (OLE) on Draw/Impress pages."""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar

from plugin.draw.base import ToolDrawSpecialBase
from plugin.framework.errors import best_effort, is_disposed_exception
from plugin.writer.math.math_mml_convert import MATH_CLSID, convert_latex_to_starmath, convert_mathml_to_starmath

log = logging.getLogger("writeragent.draw")

_MIN_W_HMM, _MIN_H_HMM = 500, 400
_MAX_W_HMM, _MAX_H_HMM = 120_000, 80_000

_DISPLAY_BLOCK_RE = re.compile(
    r"\\(?:displaystyle|int|iint|iiint|oint)(?![A-Za-z])"
)


# com.sun.star.embed.EmbedMapUnits (offapi/.../embed/EmbedMapUnits.idl).
# StarMath stores its vis-area in twips (SmMapUnit). Shape units are 1/100 mm.
_MAP_ONE_100TH_MM = 0
_MAP_ONE_10TH_MM = 1
_MAP_ONE_MM = 2
_MAP_ONE_CM = 3
_MAP_ONE_100TH_INCH = 5
_MAP_POINT = 8
_MAP_TWIP = 9
# com.sun.star.embed.Aspects.MSOLE_CONTENT
_ASPECT_CONTENT = 1

_HMM_SCALE = {
    _MAP_ONE_100TH_MM: (1, 1),
    _MAP_ONE_10TH_MM: (10, 1),
    _MAP_ONE_MM: (100, 1),
    _MAP_ONE_CM: (1000, 1),
    _MAP_ONE_100TH_INCH: (254, 10),
    _MAP_POINT: (2540, 72),
    _MAP_TWIP: (2540, 1440),
}


def _content_size_hmm(sz: Any, map_unit: int = _MAP_ONE_100TH_MM) -> tuple[int, int] | None:
    if sz is None:
        return None
    w, h = int(sz.Width), int(sz.Height)
    if w <= 0 or h <= 0:
        return None
    spec = _HMM_SCALE.get(int(map_unit))
    if spec is None:
        return None
    num, den = spec
    if den == 1:
        return w * num, h * num
    return round(w * num / den), round(h * num / den)


def _read_shape_property(shape: Any, name: str) -> Any:
    try:
        return getattr(shape, name)
    except Exception:
        log.debug("read shape property %s via getattr failed", name, exc_info=True)
    getter = getattr(shape, "getPropertyValue", None)
    if callable(getter):
        try:
            return getter(name)
        except Exception:
            return None
    return None


def _embedded_object_visual_hmm(shape: Any) -> tuple[int, int] | None:
    """``XVisualObject`` size from the ``EmbeddedObject`` property.

    ``getEmbeddedObject()`` is Writer ``TextEmbeddedObject``. Draw
    ``OLE2Shape`` does not implement it, so the call returns None
    and ``insert_math`` falls back to the length heuristic (clipped
    or oversized). The embed is the readonly ``EmbeddedObject``
    property (``svx/source/unodraw/unoprov.cxx``,
    ``OWN_ATTR_OLE_EMBEDDED_OBJECT``). Setting ``Formula`` stores
    ``SmDocShell::GetSize()`` as the vis-area
    (``SmModel::_setPropertyValues``).
    """
    try:
        emb = _read_shape_property(shape, "EmbeddedObject")
        if emb is None:
            return None
        vo = emb
        if not hasattr(vo, "getVisualAreaSize"):
            import uno

            vo = emb.queryInterface(uno.getTypeByName("com.sun.star.embed.XVisualObject"))
        if vo is None or not hasattr(vo, "getVisualAreaSize"):
            return None
        aspect = _ASPECT_CONTENT
        sz = vo.getVisualAreaSize(aspect)
        map_unit = vo.getMapUnit(aspect)
        out = _content_size_hmm(sz, map_unit)
        if out is None:
            log.debug("math_insert: unsupported embed map_unit=%s size=%sx%s", map_unit, getattr(sz, "Width", "?"), getattr(sz, "Height", "?"))
        return out
    except Exception:
        log.debug("math_insert: getVisualAreaSize path failed", exc_info=True)
    return None


def _try_ole_shape_content_size_hmm(shape: Any) -> tuple[int, int] | None:
    """Best-effort formula box in 1/100 mm, or None when UNO has no size.

    ``VisibleArea`` is already 1/100 mm. ``SvxOle2Shape``
    ``OWN_ATTR_OLE_VISAREA`` (``svx/source/unodraw/unoshap4.cxx``) converts
    ``GetOrigObjSize`` with ``Map100thMM``.
    """
    wh = _content_size_hmm(_read_shape_property(shape, "VisibleArea"))
    if wh is not None:
        return wh
    return _embedded_object_visual_hmm(shape)


def _heuristic_size_hmm(formula: str) -> tuple[int, int]:
    """Rough box from formula length when UNO sizing is unavailable."""
    n = max(8, len(formula.strip()))
    # ~550 hmm per 10 chars width, height grows slowly with line breaks
    lines = max(1, formula.count("newline") + formula.count("\n") + 1)
    return n * 55, 800 + lines * 450


class InsertMathDraw(ToolDrawSpecialBase):
    name: str | None = "insert_math"
    intent: str | None = "insert"
    description: str = "Inserts an editable LibreOffice Math formula on a Draw or Impress page. Use formula_type 'latex' or 'mathml' with the corresponding formula string; position with page, x, y (100ths of mm). Size is derived from the formula when possible."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "formula_type": {"type": "string", "enum": ["latex", "mathml"], "description": "Whether formula is LaTeX (converted via LO) or MathML."},
            "formula": {"type": "string", "description": "LaTeX or MathML formula text, per formula_type."},
            "page": {"type": "integer", "description": "Zero-based index of the draw page or slide."},
            "x": {"type": "integer", "description": "X position of the shape (100ths of mm)."},
            "y": {"type": "integer", "description": "Y position of the shape (100ths of mm)."},
        },
        "required": ["formula_type", "formula", "page", "x", "y"],
    }
    uno_services: list[str] | None = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    is_mutation: bool | None = True
    specialized_domain: ClassVar[str | None] = "math"

    def execute(self, ctx: Any, **kwargs: Any) -> Any:
        from plugin.draw.bridge import resolve_shape_page
        from com.sun.star.awt import Size, Point

        formula_type = kwargs["formula_type"]
        formula: str = kwargs["formula"].strip()
        if not formula:
            return self._tool_error("formula must be a non-empty string.")

        # None .ctx means "no inner context"; getattr(ctx, "ctx", ctx) would return None.
        uno_ctx = getattr(ctx, "ctx", None) or ctx

        if formula_type == "latex":
            display_block = bool(_DISPLAY_BLOCK_RE.search(formula))
            res = convert_latex_to_starmath(uno_ctx, formula, display_block=display_block)
        elif formula_type == "mathml":
            res = convert_mathml_to_starmath(uno_ctx, formula)
        else:
            return self._tool_error("formula_type must be 'latex' or 'mathml'.")

        if not res.ok:
            detail = (res.error_message or "").strip() or "unknown_error"
            return self._tool_error(f"Failed to convert math expression: {detail}", conversion_detail=detail)

        page, actual_idx, err = resolve_shape_page(ctx, kwargs)
        if err:
            return self._tool_error(err)
        assert page is not None

        shape = None
        added = False
        try:
            shape = ctx.doc.createInstance("com.sun.star.drawing.OLE2Shape")
            # Draw/Impress OLE: add to page first, then CLSID (see charts OLE path).
            page.add(shape)
            added = True
            shape.setPosition(Point(kwargs["x"], kwargs["y"]))
            shape.setSize(Size(_MIN_W_HMM, _MIN_H_HMM))
            shape.CLSID = MATH_CLSID

            model = shape.Model
            if model is None or not hasattr(model, "Formula"):
                raise RuntimeError("Math OLE model is not available on this build.")
            model.Formula = res.starmath

            wh = _try_ole_shape_content_size_hmm(shape)
            # A real formula can be narrower than the minimum box (a stacked
            # fraction is ~499 x 2069 hmm). Discarding that pair because one
            # side is under the floor replaced it with the length heuristic,
            # which clips the height and overshoots the width. Clamp below.
            if wh is None:
                wh = _heuristic_size_hmm(res.starmath or formula)
            w, h = wh
            w = max(_MIN_W_HMM, min(_MAX_W_HMM, w))
            h = max(_MIN_H_HMM, min(_MAX_H_HMM, h))
            shape.setSize(Size(w, h))
        except Exception as exc:
            if added:
                best_effort(lambda: page.remove(shape), "remove partial math shape")
            if is_disposed_exception(exc):
                raise
            return self._tool_error(f"Failed to insert math shape: {exc}")

        return {"status": "ok", "message": "Math formula inserted successfully", "index": page.getCount() - 1, "page": actual_idx}
