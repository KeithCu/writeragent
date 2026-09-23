# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Calc formula error detection tools.

Each tool is a ToolBase subclass that instantiates CalcBridge,
CellInspector, and ErrorDetector per call using ``ctx.doc``.
"""

import logging
from typing import Any

from plugin.calc.base import ToolCalcErrorBase

from plugin.calc.bridge import CalcBridge
from plugin.calc.inspector import CellInspector
from plugin.calc.error_detector import ErrorDetector
from plugin.framework.tool import ToolContext

log = logging.getLogger("writeragent.calc")


class DetectErrors(ToolCalcErrorBase):
    """Detect and explain formula errors in a range."""

    name = "detect_and_explain_errors"
    intent = "edit"
    description = "Detects formula errors in the specified range(s) and provides an explanation and fix suggestion. Supports lists for non-contiguous areas."
    parameters = {"type": "object", "properties": {"range": {"type": "array", "items": {"type": "string"}, "description": ('Cell range(s) to check (e.g. ["A1:Z100"] or ["A1", "B2"]). Omit or use empty list for full sheet.')}}, "required": []}
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        bridge = CalcBridge(ctx.doc)
        inspector = CellInspector(bridge)
        error_detector = ErrorDetector(bridge, inspector, ctx=ctx.ctx)
        rn = kwargs.get("range")
        if isinstance(rn, str):
            rn = [rn] if rn.strip() else []
        elif not isinstance(rn, list):
            rn = []

        if rn:
            results = [error_detector.detect_and_explain(range_str=r) for r in rn if r]
            combined_errors = []
            for res in results:
                combined_errors.extend(res.get("errors", []))
            return {"status": "ok", "result": {"error_count": len(combined_errors), "errors": combined_errors}}
        else:
            result = error_detector.detect_and_explain(range_str=None)
            return {"status": "ok", "result": result}

