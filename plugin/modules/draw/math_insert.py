from typing import Any
import logging

from plugin.framework.tool_base import ToolBase
from plugin.modules.draw.base import ToolDrawSpecialBase

log = logging.getLogger("writeragent.draw")

MATH_CLSID = "078B7ABA-54FC-457F-8551-6147e776a997"

class InsertMathDraw(ToolDrawSpecialBase):
    name = "insert_math"
    intent = "insert"
    description = "Inserts a math formula (from LaTeX or MathML) onto a Draw or Impress page. Only one of latex or mathml should be provided."
    parameters = {
        "type": "object",
        "properties": {
            "latex": {
                "type": "string",
                "description": "The LaTeX formula string.",
            },
            "mathml": {
                "type": "string",
                "description": "The MathML formula string.",
            },
            "page_index": {
                "type": "integer",
                "description": "The index of the page/slide where the formula should be inserted. Defaults to the active page.",
            },
            "x": {"type": "integer", "description": "X position (100ths of mm). Default 2000."},
            "y": {"type": "integer", "description": "Y position (100ths of mm). Default 2000."},
            "width": {"type": "integer", "description": "Width (100ths of mm). Default 5000."},
            "height": {"type": "integer", "description": "Height (100ths of mm). Default 2000."},
        },
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

        latex = kwargs.get("latex")
        mathml = kwargs.get("mathml")

        if latex and mathml:
            return self._tool_error("Provide either latex or mathml, not both.")
        if not latex and not mathml:
            return self._tool_error("Must provide either latex or mathml.")

        if latex:
            res = convert_latex_to_starmath(ctx, latex)
        else:
            res = convert_mathml_to_starmath(ctx, str(mathml))

        if not res.ok:
            return self._tool_error(f"Failed to convert math expression: {res.error_message}")

        bridge = DrawBridge(ctx.doc)
        idx = kwargs.get("page_index")
        page = (
            bridge.get_pages().getByIndex(idx)
            if idx is not None
            else bridge.get_active_page()
        )
        if page is None:
            return self._tool_error("No draw page available or invalid page index.")

        try:
            shape = ctx.doc.createInstance("com.sun.star.drawing.OLE2Shape")
            shape.CLSID = MATH_CLSID

            x = int(kwargs.get("x", 2000))
            y = int(kwargs.get("y", 2000))
            width = int(kwargs.get("width", 5000))
            height = int(kwargs.get("height", 2000))

            shape.setPosition(Point(x, y))
            shape.setSize(Size(width, height))

            page.add(shape)

            model = shape.Model
            if hasattr(model, "Formula"):
                model.Formula = res.starmath
            else:
                # OLE Model might not immediately have Formula in some execution paths
                pass

        except Exception as e:
            return self._tool_error(f"Failed to insert math shape: {e}")

        return {
            "status": "ok",
            "message": "Math formula inserted successfully",
            "shape_index": page.getCount() - 1,
            "page_index": idx if idx is not None else bridge.get_active_page_index()
        }
