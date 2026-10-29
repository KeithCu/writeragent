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

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.calc.base import ToolCalcErrorBase

from plugin.calc.bridge import CalcBridge
from plugin.calc.inspector import CellInspector
from plugin.calc.error_detector import ErrorDetector

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


class DetectErrors(ToolCalcErrorBase):
    """Detect and explain formula errors in a range."""

    name: str | None = "detect_and_explain_errors"
    intent: str | None = "edit"
    description: str = "Detects formula errors in the specified range(s) and provides an explanation and fix suggestion. Supports lists for non-contiguous areas. Explanations are capped at 50 per range; truncated and total_error_count report any remainder."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"range": {"type": "array", "items": {"type": "string"}, "description": ('Cell range(s) to check (e.g. ["A1:Z100"] or ["A1", "B2"]). Omit or use empty list for full sheet.')}}, "required": []}
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        raw = kwargs.get("range")
        if raw is None:
            ranges: list[Any] = []
        elif isinstance(raw, str):
            ranges = [raw]
        elif isinstance(raw, list):
            ranges = list(raw)
        else:
            return self._tool_error("range must be a string or a list of strings", code="INVALID_RANGE")

        for idx, entry in enumerate(ranges):
            if not isinstance(entry, str) or not entry.strip():
                return self._tool_error(f"Invalid range entry {idx}: {entry!r}", code="INVALID_RANGE")
            ranges[idx] = entry.strip()

        bridge = CalcBridge(ctx.doc)
        inspector = CellInspector(bridge)
        error_detector = ErrorDetector(bridge, inspector, ctx=ctx.ctx)

        if len(ranges) <= 1:
            return {"status": "ok", "result": error_detector.detect_and_explain(range_str=ranges[0] if ranges else None)}

        results = [error_detector.detect_and_explain(range_str=r) for r in ranges]
        combined_errors: list[dict[str, Any]] = []
        for res in results:
            combined_errors.extend(res.get("errors", []))
        total = sum(res.get("total_error_count", res.get("error_count", 0)) for res in results)
        truncated = any(res.get("truncated") for res in results)
        return {
            "status": "ok",
            "result": {
                "ranges": ranges,
                "total_error_count": total,
                "error_count": len(combined_errors),
                "truncated": truncated,
                "errors": combined_errors,
            },
        }
