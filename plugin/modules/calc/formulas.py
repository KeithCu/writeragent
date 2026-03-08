"""Calc formula error detection tools.

Each tool is a ToolBase subclass that instantiates CalcBridge,
CellInspector, and ErrorDetector per call using ``ctx.doc``.
"""

import logging

from plugin.framework.tool_base import ToolBase
from plugin.modules.calc.bridge import CalcBridge
from plugin.modules.calc.inspector import CellInspector
from plugin.modules.calc.error_detector import ErrorDetector

logger = logging.getLogger("writeragent.calc")


class DetectErrors(ToolBase):
    """Detect and explain formula errors in a range."""

    name = "detect_and_explain_errors"
    intent = "edit"
    description = (
        "Detects formula errors in the specified range(s) and provides "
        "an explanation and fix suggestion. Supports lists for "
        "non-contiguous areas."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": (
                    "Cell range(s) to check (e.g. A1:Z100) or list of "
                    "ranges/cells for non-contiguous areas. Full sheet "
                    "if empty."
                ),
            },
        },
        "required": [],
    }
    doc_types = ["calc"]
    is_mutation = False

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        inspector = CellInspector(bridge)
        error_detector = ErrorDetector(bridge, inspector)
        rn = kwargs.get("range_name")

        try:
            if rn and isinstance(rn, list):
                results = [
                    error_detector.detect_and_explain(range_str=r) for r in rn
                ]
                combined_errors = []
                for res in results:
                    combined_errors.extend(res.get("errors", []))
                return {
                    "status": "ok",
                    "result": {
                        "error_count": len(combined_errors),
                        "errors": combined_errors,
                    },
                }
            else:
                result = error_detector.detect_and_explain(range_str=rn)
                return {"status": "ok", "result": result}
        except Exception as e:
            logger.exception("detect_and_explain_errors failed")
            return {"status": "error", "error": str(e)}
