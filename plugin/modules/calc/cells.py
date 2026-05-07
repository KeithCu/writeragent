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
"""Calc cell operation tools.

Each tool is a ToolBase subclass that instantiates CalcBridge,
CellInspector, and CellManipulator per call using ``ctx.doc``.
"""

import json
import logging

from plugin.framework.errors import ToolExecutionError
from plugin.framework.tool import ToolBase
from plugin.modules.calc.bridge import CalcBridge
from plugin.modules.calc.inspector import CellInspector
from plugin.modules.calc.manipulator import CellManipulator

logger = logging.getLogger("writeragent.calc")


# ── Colour helper ──────────────────────────────────────────────────────


def _parse_color(color_str):
    """Convert a hex colour string or named colour to an RGB integer.

    Returns:
        int colour value, or *None* if *color_str* is falsy or
        unparseable.
    """
    if not color_str:
        return None
    color_str = color_str.strip().lower()
    color_names = {"red": 0xFF0000, "green": 0x00FF00, "blue": 0x0000FF, "yellow": 0xFFFF00, "white": 0xFFFFFF, "black": 0x000000, "orange": 0xFF8C00, "purple": 0x800080, "gray": 0x808080}
    if color_str in color_names:
        return color_names[color_str]
    if color_str.startswith("#"):
        try:
            return int(color_str[1:], 16)
        except ValueError:
            return None
    return None


# ── Tools ──────────────────────────────────────────────────────────────


class ReadCellRange(ToolBase):
    """Read values from one or more cell ranges."""

    name = "read_cell_range"
    description = "Reads values from the specified cell range(s). Supports lists for non-contiguous areas."
    parameters = {"type": "object", "properties": {"range_name": {"type": "array", "items": {"type": "string"}, "description": ('Cell range(s) (e.g. ["A1:D10"] or ["A1", "C2:E5"]) for one or more ranges/cells.')}}, "required": ["range_name"]}
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier = "core"
    is_mutation = False

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        inspector = CellInspector(bridge)
        rn = kwargs.get("range_name") or []
        rn = [rn] if isinstance(rn, str) else (rn or [])

        if len(rn) == 0:
            return self._tool_error("range_name is required")
        if len(rn) == 1:
            result = inspector.read_range(rn[0])
            return {"status": "ok", "result": [result]}
        results = [inspector.read_range(r) for r in rn]
        return {"status": "ok", "result": results}


class WriteCellRange(ToolBase):
    """Write formulas or values to a cell range."""

    name = "write_formula_range"
    description = "Writes formulas or values to a cell range(s) efficiently. Single string fills entire range; JSON array must match range size exactly (one value per cell). Use an empty string or empty array to clear contents. Supports lists for non-contiguous areas."
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {"type": "array", "items": {"type": "string"}, "description": ('Target range(s) (e.g. ["A1:A10"] or ["A1", "B2:D2"]) for one or more ranges.')},
            "formula_or_values": {
                "type": "string",
                "description": ("Single string: fills the entire range with that value or formula (use '=' prefix for formulas). JSON array: must have exactly as many elements as cells in the range (e.g. '[\"a\", \"b\"]' for 2 cells). Empty string/array clears the range."),
            },
        },
        "required": ["range_name", "formula_or_values"],
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier = "core"
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        rn = kwargs.get("range_name") or []
        rn = [rn] if isinstance(rn, str) else (rn or [])
        fov = kwargs.get("formula_or_values")
        # Normalize: schema is string for Gemini; accept number/list from other providers
        if isinstance(fov, (int, float)):
            fov = str(fov)
        elif isinstance(fov, list):
            fov = json.dumps(fov) if fov else ""

        if len(rn) == 0:
            return self._tool_error("range_name is required")
        if len(rn) == 1:
            result = manipulator.write_formula_range(rn[0], fov)
            return {"status": "ok", "message": result}
        for r in rn:
            manipulator.write_formula_range(r, fov)
        return {"status": "ok", "message": f"Wrote to {len(rn)} ranges"}


class InsertCellHtml(ToolBase):
    """Insert HTML as rich text into a single cell (active sheet)."""

    name = "insert_cell_html"
    intent = "edit"
    description = "Parses HTML with the same filter as Writer and pastes rich text into one cell on the active sheet (e.g. <b>, <i>, <a href>, line breaks). Does not support images or embedded objects. Clears existing cell text."
    parameters = {"type": "object", "properties": {"cell_address": {"type": "string", "description": 'Single cell (e.g. "A1") on the active sheet.'}, "html": {"type": "string", "description": "HTML fragment or small document (UTF-8)."}}, "required": ["cell_address", "html"]}
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier = "extended"
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from plugin.modules.calc.address_utils import parse_address
        from plugin.modules.calc.rich_html import insert_cell_html_rich

        addr = (kwargs.get("cell_address") or "").strip()
        html = kwargs.get("html")
        if not addr:
            return self._tool_error("cell_address is required")
        try:
            parse_address(addr)
        except ValueError as e:
            return self._tool_error(f"Invalid cell address: {e}")

        config_svc = None
        if ctx.services is not None and hasattr(ctx.services, "get"):
            config_svc = ctx.services.get("config")

        try:
            insert_cell_html_rich(ctx.doc, ctx.ctx, addr, html if isinstance(html, str) else "", config_svc=config_svc)
        except ToolExecutionError as e:
            return self._tool_error(str(e))

        return {"status": "ok", "message": f"Inserted rich HTML into cell {addr.upper()}."}


class SetCellStyle(ToolBase):
    """Apply style and formatting to cells or ranges."""

    name = "set_style"
    intent = "edit"
    description = "Applies style and formatting to the specified cell(s) or range(s). Supports lists for non-contiguous areas."
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {"type": "array", "items": {"type": "string"}, "description": ('Target cell(s) or range(s) (e.g. ["A1:D10"] or ["A1", "B2"]).')},
            "bold": {"type": "boolean", "description": "Bold font"},
            "italic": {"type": "boolean", "description": "Italic font"},
            "font_size": {"type": "number", "description": "Font size (points)"},
            "bg_color": {"type": "string", "description": "Background color (hex: #FF0000 or name: yellow)"},
            "font_color": {"type": "string", "description": "Font color (hex: #000000 or name: red)"},
            "h_align": {"type": "string", "enum": ["left", "center", "right", "justify"], "description": "Horizontal alignment"},
            "v_align": {"type": "string", "enum": ["top", "center", "bottom"], "description": "Vertical alignment"},
            "wrap_text": {"type": "boolean", "description": "Wrap text"},
            "border_color": {"type": "string", "description": ("Border color (hex or name). Draws a frame around the cell/range.")},
            "number_format": {"type": "string", "description": "Number format (e.g. #,##0.00, 0%, dd.mm.yyyy)"},
        },
        "required": ["range_name"],
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        rn = kwargs.get("range_name") or []
        rn = [rn] if isinstance(rn, str) else (rn or [])

        # Strict color validation: callers/tests expect invalid color strings
        # to produce a consistent `{status:"error"}` payload rather than
        # silently treating unparseable values as "no change".
        def _parse_or_error(color_key: str):
            raw = kwargs.get(color_key)
            if raw is None:
                return None
            if isinstance(raw, str) and raw.strip() == "":
                return None
            if not isinstance(raw, str):
                return None  # schema should be string, but don't hard-fail
            parsed = _parse_color(raw)
            if parsed is None:
                return {"__error__": f"Invalid {color_key}: '{raw}'"}
            return parsed

        _bg = _parse_or_error("bg_color")
        if isinstance(_bg, dict):
            return self._tool_error(_bg["__error__"])
        bg_color: int | None = _bg

        _fc = _parse_or_error("font_color")
        if isinstance(_fc, dict):
            return self._tool_error(_fc["__error__"])
        font_color: int | None = _fc

        _bc = _parse_or_error("border_color")
        if isinstance(_bc, dict):
            return self._tool_error(_bc["__error__"])
        border_color: int | None = _bc

        style_kwargs = {
            "bold": kwargs.get("bold"),
            "italic": kwargs.get("italic"),
            "bg_color": bg_color,
            "font_color": font_color,
            "font_size": kwargs.get("font_size"),
            "h_align": kwargs.get("h_align"),
            "v_align": kwargs.get("v_align"),
            "wrap_text": kwargs.get("wrap_text"),
            "border_color": border_color,
            "number_format": kwargs.get("number_format"),
        }

        if len(rn) == 0:
            return self._tool_error("range_name is required")
        if len(rn) == 1:
            manipulator.set_cell_style(rn[0], **style_kwargs)
            return {"status": "ok", "message": f"Style applied to {rn[0]}"}
        for r in rn:
            manipulator.set_cell_style(r, **style_kwargs)
        return {"status": "ok", "message": f"Style applied to {len(rn)} ranges"}


class MergeCells(ToolBase):
    """Merge a cell range."""

    name = "merge_cells"
    intent = "edit"
    description = "Merges the specified cell range(s). Typically used for main headers. Write text with write_formula_range and style with set_style after merging. Supports lists for non-contiguous areas."
    parameters = {"type": "object", "properties": {"range_name": {"type": "array", "items": {"type": "string"}, "description": ('Range(s) to merge (e.g. ["A1:D1"] or ["A1:B1", "C1:D1"]).')}, "center": {"type": "boolean", "description": "Center content (default: true)"}}, "required": ["range_name"]}
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        rn = kwargs.get("range_name") or []
        rn = [rn] if isinstance(rn, str) else (rn or [])
        center = kwargs.get("center", True)

        if len(rn) == 0:
            return self._tool_error("range_name is required")
        if len(rn) == 1:
            manipulator.merge_cells(rn[0], center=center)
            return {"status": "ok", "message": f"Merged cells {rn[0]}"}
        for r in rn:
            manipulator.merge_cells(r, center=center)
        return {"status": "ok", "message": f"Merged cells in {len(rn)} ranges"}


class SortRange(ToolBase):
    """Sort a range by a column."""

    name = "sort_range"
    intent = "edit"
    description = "Sorts the specified range(s) by a column. Use for ordering rows by values in one column. Supports lists for non-contiguous areas."
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {"type": "array", "items": {"type": "string"}, "description": ('Range(s) to sort (e.g. ["A1:D10"] or ["A1:B10", "D1:E10"]).')},
            "sort_column": {"type": "integer", "description": ("0-based column index within the range to sort by (default: 0)")},
            "ascending": {"type": "boolean", "description": ("True for ascending, False for descending (default: true)")},
            "has_header": {"type": "boolean", "description": ("Is the first row a header that should not be sorted? (default: true)")},
        },
        "required": ["range_name"],
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        rn = kwargs.get("range_name") or []
        rn = [rn] if isinstance(rn, str) else (rn or [])
        sort_column = kwargs.get("sort_column", 0)
        ascending = kwargs.get("ascending", True)
        has_header = kwargs.get("has_header", True)

        if len(rn) == 0:
            return self._tool_error("range_name is required")
        if len(rn) == 1:
            result = manipulator.sort_range(rn[0], sort_column=sort_column, ascending=ascending, has_header=has_header)
            return {"status": "ok", "message": result}
        for r in rn:
            manipulator.sort_range(r, sort_column=sort_column, ascending=ascending, has_header=has_header)
        return {"status": "ok", "message": f"Sorted {len(rn)} ranges"}


class DeleteStructure(ToolBase):
    """Delete rows or columns."""

    name = "delete_structure"
    intent = "edit"
    description = "Deletes rows or columns. Use for structural changes; prefer ranges for data operations."
    parameters = {
        "type": "object",
        "properties": {
            "structure_type": {"type": "string", "enum": ["rows", "columns"], "description": "Type of structure to delete."},
            "start": {"type": "string", "description": ('For rows: 1-based row number (e.g. "5"); for columns: column letter (e.g. "C").')},
            "count": {"type": "integer", "description": "Number to delete (default 1)."},
        },
        "required": ["structure_type", "start"],
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        structure_type = kwargs["structure_type"]
        start_raw = kwargs["start"]
        count = kwargs.get("count", 1)
        # Normalize: rows accept integer or string; columns accept letter(s).
        start = int(start_raw) if structure_type == "rows" and str(start_raw).isdigit() else start_raw

        result = manipulator.delete_structure(structure_type, start, count=count)
        return {"status": "ok", "message": result}
