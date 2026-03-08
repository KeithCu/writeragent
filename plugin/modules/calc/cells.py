"""Calc cell operation tools.

Each tool is a ToolBase subclass that instantiates CalcBridge,
CellInspector, and CellManipulator per call using ``ctx.doc``.
"""

import logging

from plugin.framework.tool_base import ToolBase
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
    color_names = {
        "red": 0xFF0000, "green": 0x00FF00, "blue": 0x0000FF,
        "yellow": 0xFFFF00, "white": 0xFFFFFF, "black": 0x000000,
        "orange": 0xFF8C00, "purple": 0x800080, "gray": 0x808080,
    }
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
    description = (
        "Reads values from the specified cell range(s). "
        "Supports lists for non-contiguous areas."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": (
                    "Cell range(s) (e.g. A1:D10, Sheet1.A1:C5) or list of "
                    "ranges/cells for non-contiguous areas."
                ),
            },
        },
        "required": ["range_name"],
    }
    doc_types = ["calc"]
    tier = "core"
    is_mutation = False

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        inspector = CellInspector(bridge)
        rn = kwargs["range_name"]

        try:
            if isinstance(rn, list):
                results = [inspector.read_range(r) for r in rn]
                return {"status": "ok", "result": results}
            else:
                result = inspector.read_range(rn)
                return {"status": "ok", "result": result}
        except Exception as e:
            logger.exception("read_cell_range failed")
            return {"status": "error", "error": str(e)}


class WriteCellRange(ToolBase):
    """Write formulas or values to a cell range."""

    name = "write_formula_range"
    description = (
        "Writes formulas or values to a cell range(s) efficiently. "
        "Use a single value to fill the entire range, or an array of "
        "values for each cell. Supports lists for non-contiguous areas."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": (
                    "Target range(s) (e.g. A1:A10, B2:D2) or list of "
                    "ranges/cells for non-contiguous areas."
                ),
            },
            "formula_or_values": {
                "type": ["string", "number", "array"],
                "description": (
                    "Single formula/value for all cells, or array of "
                    "formulas/values for each cell. Formulas start with '='."
                ),
            },
        },
        "required": ["range_name", "formula_or_values"],
    }
    doc_types = ["calc"]
    tier = "core"
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        rn = kwargs["range_name"]
        fov = kwargs["formula_or_values"]

        try:
            if isinstance(rn, list):
                for r in rn:
                    manipulator.write_formula_range(r, fov)
                return {"status": "ok", "message": f"Wrote to {len(rn)} ranges"}
            else:
                result = manipulator.write_formula_range(rn, fov)
                return {"status": "ok", "message": result}
        except Exception as e:
            logger.exception("write_formula_range failed")
            return {"status": "error", "error": str(e)}


class SetCellStyle(ToolBase):
    """Apply style and formatting to cells or ranges."""

    name = "set_cell_style"
    intent = "edit"
    description = (
        "Applies style and formatting to the specified cell(s) or "
        "range(s). Supports lists for non-contiguous areas."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": (
                    "Target cell(s) or range(s) (e.g. A1:D10) or list of "
                    "ranges/cells for non-contiguous areas."
                ),
            },
            "bold": {"type": "boolean", "description": "Bold font"},
            "italic": {"type": "boolean", "description": "Italic font"},
            "font_size": {"type": "number", "description": "Font size (points)"},
            "bg_color": {
                "type": "string",
                "description": "Background color (hex: #FF0000 or name: yellow)",
            },
            "font_color": {
                "type": "string",
                "description": "Font color (hex: #000000 or name: red)",
            },
            "h_align": {
                "type": "string",
                "enum": ["left", "center", "right", "justify"],
                "description": "Horizontal alignment",
            },
            "v_align": {
                "type": "string",
                "enum": ["top", "center", "bottom"],
                "description": "Vertical alignment",
            },
            "wrap_text": {"type": "boolean", "description": "Wrap text"},
            "border_color": {
                "type": "string",
                "description": (
                    "Border color (hex or name). Draws a frame around "
                    "the cell/range."
                ),
            },
            "number_format": {
                "type": "string",
                "description": "Number format (e.g. #,##0.00, 0%, dd.mm.yyyy)",
            },
        },
        "required": ["range_name"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        rn = kwargs["range_name"]

        style_kwargs = {
            "bold": kwargs.get("bold"),
            "italic": kwargs.get("italic"),
            "bg_color": _parse_color(kwargs.get("bg_color")),
            "font_color": _parse_color(kwargs.get("font_color")),
            "font_size": kwargs.get("font_size"),
            "h_align": kwargs.get("h_align"),
            "v_align": kwargs.get("v_align"),
            "wrap_text": kwargs.get("wrap_text"),
            "border_color": _parse_color(kwargs.get("border_color")),
            "number_format": kwargs.get("number_format"),
        }

        try:
            if isinstance(rn, list):
                for r in rn:
                    manipulator.set_cell_style(r, **style_kwargs)
                return {
                    "status": "ok",
                    "message": f"Style applied to {len(rn)} ranges",
                }
            else:
                manipulator.set_cell_style(rn, **style_kwargs)
                return {"status": "ok", "message": f"Style applied to {rn}"}
        except Exception as e:
            logger.exception("set_cell_style failed")
            return {"status": "error", "error": str(e)}


class MergeCells(ToolBase):
    """Merge a cell range."""

    name = "merge_cells"
    intent = "edit"
    description = (
        "Merges the specified cell range(s). Typically used for main "
        "headers. Write text with write_formula_range and style with "
        "set_cell_style after merging. Supports lists for non-contiguous "
        "areas."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": (
                    "Range(s) to merge (e.g. A1:D1) or list of ranges for "
                    "non-contiguous areas."
                ),
            },
            "center": {
                "type": "boolean",
                "description": "Center content (default: true)",
            },
        },
        "required": ["range_name"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        rn = kwargs["range_name"]
        center = kwargs.get("center", True)

        try:
            if isinstance(rn, list):
                for r in rn:
                    manipulator.merge_cells(r, center=center)
                return {
                    "status": "ok",
                    "message": f"Merged cells in {len(rn)} ranges",
                }
            else:
                manipulator.merge_cells(rn, center=center)
                return {"status": "ok", "message": f"Merged cells {rn}"}
        except Exception as e:
            logger.exception("merge_cells failed")
            return {"status": "error", "error": str(e)}


class ClearRange(ToolBase):
    """Clear all contents in a cell range."""

    name = "clear_range"
    intent = "edit"
    description = (
        "Clears all contents (values, formulas) in the specified "
        "range(s). Supports lists for non-contiguous areas."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": (
                    "Range(s) to clear (e.g. A1:D10) or list of "
                    "ranges/cells for non-contiguous areas."
                ),
            },
        },
        "required": ["range_name"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        rn = kwargs["range_name"]

        try:
            if isinstance(rn, list):
                for r in rn:
                    manipulator.clear_range(r)
                return {
                    "status": "ok",
                    "message": f"Cleared {len(rn)} ranges",
                }
            else:
                manipulator.clear_range(rn)
                return {"status": "ok", "message": f"Cleared range {rn}"}
        except Exception as e:
            logger.exception("clear_range failed")
            return {"status": "error", "error": str(e)}


class SortRange(ToolBase):
    """Sort a range by a column."""

    name = "sort_range"
    intent = "edit"
    description = (
        "Sorts the specified range(s) by a column. Use for ordering "
        "rows by values in one column. Supports lists for non-contiguous "
        "areas."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": (
                    "Range(s) to sort (e.g. A1:D10) or list of ranges "
                    "for non-contiguous areas."
                ),
            },
            "sort_column": {
                "type": "integer",
                "description": (
                    "0-based column index within the range to sort by "
                    "(default: 0)"
                ),
            },
            "ascending": {
                "type": "boolean",
                "description": (
                    "True for ascending, False for descending (default: true)"
                ),
            },
            "has_header": {
                "type": "boolean",
                "description": (
                    "Is the first row a header that should not be sorted? "
                    "(default: true)"
                ),
            },
        },
        "required": ["range_name"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        rn = kwargs["range_name"]
        sort_column = kwargs.get("sort_column", 0)
        ascending = kwargs.get("ascending", True)
        has_header = kwargs.get("has_header", True)

        try:
            if isinstance(rn, list):
                for r in rn:
                    manipulator.sort_range(
                        r, sort_column=sort_column,
                        ascending=ascending, has_header=has_header,
                    )
                return {
                    "status": "ok",
                    "message": f"Sorted {len(rn)} ranges",
                }
            else:
                result = manipulator.sort_range(
                    rn, sort_column=sort_column,
                    ascending=ascending, has_header=has_header,
                )
                return {"status": "ok", "message": result}
        except Exception as e:
            logger.exception("sort_range failed")
            return {"status": "error", "error": str(e)}


class ImportCsv(ToolBase):
    """Import CSV data into the sheet."""

    name = "import_csv_from_string"
    intent = "edit"
    description = (
        "Inserts CSV data into the sheet starting at a cell. "
        "Handles large datasets efficiently."
    )
    parameters = {
        "type": "object",
        "properties": {
            "csv_data": {
                "type": "string",
                "description": "CSV content as string (rows separated by \\n).",
            },
            "target_cell": {
                "type": "string",
                "description": "Starting cell (default 'A1').",
            },
        },
        "required": ["csv_data"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        csv_data = kwargs["csv_data"]
        target_cell = kwargs.get("target_cell", "A1")

        try:
            result = manipulator.import_csv_from_string(csv_data, target_cell=target_cell)
            return {"status": "ok", "message": result}
        except Exception as e:
            logger.exception("import_csv_from_string failed")
            return {"status": "error", "error": str(e)}


class DeleteStructure(ToolBase):
    """Delete rows or columns."""

    name = "delete_structure"
    intent = "edit"
    description = (
        "Deletes rows or columns. Use for structural changes; "
        "prefer ranges for data operations."
    )
    parameters = {
        "type": "object",
        "properties": {
            "structure_type": {
                "type": "string",
                "enum": ["rows", "columns"],
                "description": "Type of structure to delete.",
            },
            "start": {
                "type": ["integer", "string"],
                "description": (
                    "For rows: row number (1-based); for columns: column letter."
                ),
            },
            "count": {
                "type": "integer",
                "description": "Number to delete (default 1).",
            },
        },
        "required": ["structure_type", "start"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        structure_type = kwargs["structure_type"]
        start = kwargs["start"]
        count = kwargs.get("count", 1)

        try:
            result = manipulator.delete_structure(structure_type, start, count=count)
            return {"status": "ok", "message": result}
        except Exception as e:
            logger.exception("delete_structure failed")
            return {"status": "error", "error": str(e)}
