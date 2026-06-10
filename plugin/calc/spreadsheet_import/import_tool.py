# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc chat tool for converting spreadsheet formulas to Python."""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.tool import ToolBaseDummy
from plugin.calc.spreadsheet_import.import_dialog import run_sheet_conversion

log = logging.getLogger("writeragent.calc.import_tool")


# Hidden from LLM/MCP tool lists (ToolBaseDummy). Menu path still uses run_sheet_conversion
# via import_dialog.show_import_dialog. Re-enable: change base class back to ToolBase.
class ConvertSpreadsheetToPython(ToolBaseDummy):
    """Convert spreadsheet formulas to =PY() Python cells."""

    name = "convert_spreadsheet_to_python"
    description = (
        "Converts legacy Calc spreadsheet formulas to `=PY()` Python formulas "
        "retaining cell constant values and number formats."
    )
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["sheet", "selection"],
                "description": "Conversion scope: 'sheet' for the entire active sheet, or 'selection' for selected cells.",
            },
            "output_mode": {
                "type": "string",
                "enum": ["new_sheet", "in_place"],
                "description": "Where to place the converted cells: 'new_sheet' creates a new sheet 'PythonImport', 'in_place' modifies active sheet.",
            },
            "vectorize": {
                "type": "boolean",
                "description": "If True, auto-vectorizes homogeneous columns into single array-formulas when safe.",
            },
            "verify": {
                "type": "boolean",
                "description": "If True, forces Calc recalc and verifies converted cell values against original values.",
            },
        },
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier = "core"
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        if doc is None:
            return self._tool_error("No active document context")

        # Get active sheet
        controller = doc.getCurrentController()
        source_sheet = controller.getActiveSheet()
        if source_sheet is None:
            return self._tool_error("No active sheet in spreadsheet")

        scope = kwargs.get("scope", "sheet")
        output_mode = kwargs.get("output_mode", "new_sheet")
        vectorize = kwargs.get("vectorize", True)
        verify = kwargs.get("verify", True)

        try:
            res = run_sheet_conversion(
                ctx.ctx,
                doc,
                source_sheet,
                scope=scope,
                output_mode=output_mode,
                vectorize=vectorize,
                verify=verify,
            )
            return {"status": "ok", **res}
        except Exception as e:
            log.exception("Chat tool convert_spreadsheet_to_python execution failed")
            return self._tool_error(f"Failed to convert spreadsheet: {e}")
