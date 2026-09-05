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


# crosshair: off
import json
import logging
from typing import Any

from plugin.framework.errors import ToolExecutionError
from plugin.framework.tool import ToolBase
from plugin.calc.address_utils import index_to_column, split_sheet_prefix
from plugin.calc.bridge import CalcBridge
from plugin.calc.base import ToolCalcRangeBase
from plugin.calc.inspector import CellInspector
from plugin.calc.manipulator import CellManipulator

# Verbose per-cell dicts blow chat/tool context (issue 405: A1:H500 → HTTP 400).
# Inspector.read_range stays uncapped for UNO tests and internal callers.
_READ_CELL_RANGE_MAX_CELLS = 80
_READ_CELL_RANGE_PREVIEW_ROWS = 10
_READ_CELL_RANGE_TRUNCATED_MSG = (
    "Range is too large to load into chat (would overload the model context). "
    "The sample below is a peek only — pass this A1 address to =PY instead of re-reading."
)

from plugin.doc.visual_helpers import parse_color_to_uno_int
from plugin.framework.deal_shim import deal

log = logging.getLogger("writeragent.calc")


# ── Colour helper ──────────────────────────────────────────────────────


@deal.post(lambda result: result is None or (isinstance(result, int) and 0 <= result <= 0xFFFFFF))
def _parse_color(color_str):
    """Convert a hex colour string or named colour to an RGB integer.

    String-only wrapper around :func:`parse_color_to_uno_int` so Calc ``set_style``
    keeps rejecting non-string LLM args (ints/tuples) instead of accepting them.
    """
    if not color_str:
        return None
    if not isinstance(color_str, str):
        return None
    return parse_color_to_uno_int(color_str)


def _format_sheet_address(range_name: str, local_addr: str) -> str:
    """Keep the original sheet prefix (dot or bang, quoted or not) on a clipped address."""
    sheet, _unused_local = split_sheet_prefix(range_name)
    if not sheet:
        return local_addr
    quoted = range_name.lstrip().startswith("'")
    sep = "!" if "!" in range_name else "."
    name = f"'{sheet}'" if quoted else sheet
    return f"{name}{sep}{local_addr}"


def _preview_if_large(bridge, range_name: str) -> dict[str, Any] | None:
    """Return clip metadata when the range is too big for a full chat dump, else None."""
    try:
        cell_range = bridge.resolve_range_or_address(range_name)
        if not hasattr(cell_range, "getRangeAddress"):
            return None
        addr = cell_range.getRangeAddress()
        rows = int(addr.EndRow) - int(addr.StartRow) + 1
        cols = int(addr.EndColumn) - int(addr.StartColumn) + 1
        cells = rows * cols
        if cells <= _READ_CELL_RANGE_MAX_CELLS:
            return None
        preview_rows = min(rows, _READ_CELL_RANGE_PREVIEW_ROWS)
        end_row = int(addr.StartRow) + preview_rows - 1
        local = (
            f"{index_to_column(int(addr.StartColumn))}{int(addr.StartRow) + 1}:"
            f"{index_to_column(int(addr.EndColumn))}{end_row + 1}"
        )
        return {
            "rows": rows,
            "columns": cols,
            "cells": cells,
            "preview_range": _format_sheet_address(range_name, local),
        }
    except Exception:
        log.exception("Could not size range %s for read_cell_range cap; reading in full", range_name)
        return None


class ReadCellRange(ToolBase):
    """Read values from one or more cell ranges."""

    name = "read_cell_range"
    description = (
        "Reads values from the specified cell range(s). Inspection only — keep ranges small "
        "(headers or a few dozen cells). A large dump overloads chat context; for bulk work "
        "write =PY(..., DataRange) instead of reading the block. Date/time-formatted numeric "
        "cells return an ISO 8601 string in `value` with `type` and `format_category` of date, "
        "time, or datetime, plus `format_code` (Calc FormatString, observability only). "
        "Elapsed/stopwatch formats (`[HH]:MM:SS`, …) return `PTnHnMnS` (e.g. PT30H) with "
        "type/format_category duration. Supports lists for non-contiguous areas."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    'Cell range(s) (e.g. ["A1:D10"], ["Sheet1.A1:C5"], '
                    '["\'Data Sheet\'!B2"]). Sheet prefixes target that sheet '
                    "without switching the active sheet."
                ),
            }
        },
        "required": ["range"],
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier = "core"
    is_mutation = False

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        inspector = CellInspector(bridge)
        rn = kwargs.get("range") or []
        rn = [rn] if isinstance(rn, str) else (rn or [])

        if len(rn) == 0:
            return self._tool_error("range is required")
        if len(rn) == 1:
            return self._read_one(bridge, inspector, rn[0])
        results = [self._read_one(bridge, inspector, r) for r in rn]
        # Preserve the old list-of-grids shape when nothing was truncated.
        if all(item.get("status") == "ok" and not item.get("truncated") for item in results):
            return {"status": "ok", "result": [item["result"][0] for item in results]}
        return {"status": "ok", "result": results}

    def _read_one(self, bridge, inspector, range_name: str) -> dict:
        """Read one range; preview-only when the full grid would swamp chat context."""
        preview = _preview_if_large(bridge, range_name)
        if preview is None:
            grid = inspector.read_range(range_name, include_format_info=True)
            return {"status": "ok", "result": [grid]}
        grid = inspector.read_range(preview["preview_range"], include_format_info=True)
        return {
            "status": "ok",
            "truncated": True,
            "message": _READ_CELL_RANGE_TRUNCATED_MSG,
            "range": range_name,
            "preview_range": preview["preview_range"],
            "rows": preview["rows"],
            "columns": preview["columns"],
            "cells": preview["cells"],
            "result": [grid],
        }


class WriteCellRange(ToolBase):
    """Write formulas or values to a cell range."""

    name = "write_formula_range"
    description = (
        'To run Python on sheet data, write =PY("result = …"; DataRange) into one empty cell '
        "outside DataRange (e.g. J1 for A1:H500, or a new sheet). That cell spills the 2D result "
        "(values are in the neighbors). A small peek of the origin or headers is enough — do not "
        "dump the input or full spill into chat; do not write =PY onto DataRange (circular). If "
        "they asked for in-place unique rows, still land beside/new sheet and say where. "
        'Tables (headers, mixed types): =PY("result = data.to_pandas().drop_duplicates()"; DataRange). '
        'Always use data.to_pandas() rather than pd.DataFrame(data) because to_pandas() uses row 0 as column headers; '
        'pd.DataFrame(data) treats headers as data and generates synthetic numeric columns (0..N) that spill as a junk top row. '
        "np.unique on mixed rows fails — NumPy object arrays cannot compare/hash mixed cell types. "
        "Writes formulas or values to a cell range(s) efficiently. Single string fills entire range; "
        "JSON array must match range size exactly (one value per cell); or multiline CSV from a start "
        "cell. Use an empty string or empty array to clear contents. Supports lists for non-contiguous "
        "areas. Prefer plain values/ISO "
        "dates for static cells; use an '=' formula only when the cell must stay live (e.g. TODAY(), "
        "computed duration). Dates and times: use ISO 8601 only — YYYY-MM-DD, HH:MM[:SS], or "
        "YYYY-MM-DDTHH:MM[:SS]. These become real Calc date/time values. Elapsed/stopwatch values: "
        "use PTnHnMnS (e.g. PT30H, PT1H30M); these become duration serials with elapsed formatting. "
        "Do not include a timezone offset or Z, and do not use locale forms like 08/05/2026; those "
        "are stored as text. Prefix with an apostrophe ('2026-08-08) to force text."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    'Target range(s) (e.g. ["A1:A10"], ["Sheet1.B2:D2"]). '
                    "Use a dot for other sheets (Sheet1.B2), never Excel Sheet1!B2. "
                    "Sheet prefixes target that sheet without switching the active sheet."
                ),
            },
            "values": {
                "type": "string",
                "description": (
                    "Single string: fills the entire range with that value or formula "
                    "(use '=' prefix for formulas). In formulas, other sheets are Sheet.A1 "
                    "(dot), not Excel Sheet!A1. JSON array: must have exactly as many "
                    "elements as cells in the range (e.g. '[\"a\", \"b\"]' for 2 cells). "
                    "Empty string/array clears the range."
                ),
            },
        },
        "required": ["range", "values"],
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier = "core"
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from plugin.writer.edit_review import WriterCompoundUndo

        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        rn = kwargs.get("range") or []
        rn = [rn] if isinstance(rn, str) else (rn or [])
        fov = kwargs.get("values")
        # Normalize: schema is string for Gemini; accept number/list from other providers
        if isinstance(fov, (int, float)):
            fov = str(fov)
        elif isinstance(fov, list):
            fov = json.dumps(fov) if fov else ""

        if len(rn) == 0:
            return self._tool_error("range is required")

        undo = WriterCompoundUndo(ctx.doc, "WriterAgent: Write formulas")
        try:
            if len(rn) == 1:
                result = manipulator.write_formula_range(rn[0], fov)
                return {"status": "ok", "message": result}
            for r in rn:
                manipulator.write_formula_range(r, fov)
            return {"status": "ok", "message": f"Wrote to {len(rn)} ranges"}
        except Exception as e:
            return self._tool_error(str(e))
        finally:
            undo.close()


class InsertCellHtml(ToolBase):
    """Insert HTML as rich text into a single cell (active sheet)."""

    name = "insert_cell_html"
    intent = "edit"
    description = (
        "Parses HTML with the same filter as Writer and pastes rich text into one cell on the "
        "active sheet (e.g. <b>, <i>, <a href>, line breaks). Does not support images or embedded "
        "objects. Clears existing cell text. Use set_style for table-wide borders."
    )
    parameters = {"type": "object", "properties": {"cell": {"type": "string", "description": 'Single cell (e.g. "A1") on the active sheet.'}, "html": {"type": "string", "description": "HTML fragment or small document (UTF-8)."}}, "required": ["cell", "html"]}
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from plugin.calc.address_utils import parse_address
        from plugin.calc.rich_html import insert_cell_html_rich

        addr = (kwargs.get("cell") or "").strip()
        html = kwargs.get("html")
        if not addr:
            return self._tool_error("cell is required")
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
            "range": {"type": "array", "items": {"type": "string"}, "description": ('Target cell(s) or range(s) (e.g. ["A1:D10"] or ["A1", "B2"]).')},
            "bold": {"type": "boolean", "description": "Bold font"},
            "italic": {"type": "boolean", "description": "Italic font"},
            "font_size": {"type": "number", "description": "Font size (points)"},
            "bg_color": {"type": "string", "description": "Background color (hex: #FF0000 or name: yellow)"},
            "font_color": {"type": "string", "description": "Font color (hex: #000000 or name: red)"},
            "h_align": {"type": "string", "enum": ["left", "center", "right", "justify"], "description": "Horizontal alignment"},
            "v_align": {"type": "string", "enum": ["top", "center", "bottom"], "description": "Vertical alignment"},
            "wrap_text": {"type": "boolean", "description": "Wrap text"},
            "border_color": {"type": "string", "description": ("Border color (hex or name). Draws a frame around the cell/range.")},
        },
        "required": ["range"],
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation = True
    # Kept for scripting API / in-process callers; omitted from LLM schema so models cannot
    # casually rewrite NumberFormat via set_style (see docs/calc/date-time-handling.md S26).
    scripting_only_parameters = frozenset({"number_format"})

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        rn = kwargs.get("range") or []
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

        # Present-but-wrong-type args must error (not silently become None): ToolBase.validate
        # does not check JSON-schema types, and a successful no-op hides the mistake from the model.
        def _optional_bool(key: str) -> tuple[bool | None, str | None]:
            if key not in kwargs:
                return None, None
            raw = kwargs[key]
            if raw is None:
                return None, None
            if not isinstance(raw, bool):
                return None, f"{key} must be a boolean (true or false), got {type(raw).__name__}"
            return raw, None

        def _optional_font_size() -> tuple[float | None, str | None]:
            if "font_size" not in kwargs:
                return None, None
            raw = kwargs["font_size"]
            if raw is None:
                return None, None
            # bool is a subclass of int; reject it so true/false cannot become 1.0/0.0.
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return None, f"font_size must be a number, got {type(raw).__name__}"
            return float(raw), None

        def _optional_str(key: str) -> tuple[str | None, str | None]:
            if key not in kwargs:
                return None, None
            raw = kwargs[key]
            if raw is None:
                return None, None
            if not isinstance(raw, str):
                return None, f"{key} must be a string, got {type(raw).__name__}"
            if not raw.strip():
                return None, None
            return raw, None

        bold, err = _optional_bool("bold")
        if err:
            return self._tool_error(err)
        italic, err = _optional_bool("italic")
        if err:
            return self._tool_error(err)
        wrap_text, err = _optional_bool("wrap_text")
        if err:
            return self._tool_error(err)
        font_size, err = _optional_font_size()
        if err:
            return self._tool_error(err)
        h_align, err = _optional_str("h_align")
        if err:
            return self._tool_error(err)
        v_align, err = _optional_str("v_align")
        if err:
            return self._tool_error(err)
        # number_format: scripting_only_parameters; omitted from LLM schema.
        number_format, err = _optional_str("number_format")
        if err:
            return self._tool_error(err)

        style_kwargs: dict[str, Any] = {
            "bold": bold,
            "italic": italic,
            "bg_color": bg_color,
            "font_color": font_color,
            "font_size": font_size,
            "h_align": h_align,
            "v_align": v_align,
            "wrap_text": wrap_text,
            "border_color": border_color,
            "number_format": number_format,
        }

        if len(rn) == 0:
            return self._tool_error("range is required")
        try:
            if len(rn) == 1:
                manipulator.set_cell_style(rn[0], **style_kwargs)
                return {"status": "ok", "message": f"Style applied to {rn[0]}"}
            for r in rn:
                manipulator.set_cell_style(r, **style_kwargs)
            return {"status": "ok", "message": f"Style applied to {len(rn)} ranges"}
        except Exception as e:
            return self._tool_error(str(e))


class MergeCells(ToolBase):
    """Merge a cell range."""

    name = "merge_cells"
    intent = "edit"
    description = "Merges the specified cell range(s). Typically used for main headers. Write text with write_formula_range and style with set_style after merging. Supports lists for non-contiguous areas."
    parameters = {"type": "object", "properties": {"range": {"type": "array", "items": {"type": "string"}, "description": ('Range(s) to merge (e.g. ["A1:D1"] or ["A1:B1", "C1:D1"]).')}, "center": {"type": "boolean", "description": "Center content (default: true)"}}, "required": ["range"]}
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        rn = kwargs.get("range") or []
        rn = [rn] if isinstance(rn, str) else (rn or [])
        center = kwargs.get("center", True)

        if len(rn) == 0:
            return self._tool_error("range is required")
        try:
            if len(rn) == 1:
                manipulator.merge_cells(rn[0], center=center)
                return {"status": "ok", "message": f"Merged cells {rn[0]}"}
            for r in rn:
                manipulator.merge_cells(r, center=center)
            return {"status": "ok", "message": f"Merged cells in {len(rn)} ranges"}
        except Exception as e:
            return self._tool_error(str(e))


class SortRange(ToolCalcRangeBase):
    """Sort a range by a column."""

    name = "sort_range"
    intent = "edit"
    description = (
        "Stable one-column sort of the specified range(s) by values in one column. "
        "Multi-key sorts are multiple calls (two stable one-column passes). "
        "Do pass has_header=true when row 1 is labels because otherwise labels "
        "sort as values. "
        "Supports lists for non-contiguous areas."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range": {"type": "array", "items": {"type": "string"}, "description": ('Range(s) to sort (e.g. ["A1:D10"] or ["A1:B10", "D1:E10"]).')},
            "sort_column": {"type": "integer", "description": ("0-based column index within the range to sort by (default: 0)")},
            "ascending": {"type": "boolean", "description": ("True for ascending, False for descending (default: true)")},
            "has_header": {
                "type": "boolean",
                "description": (
                    "true when row 1 is labels; false only for a headerless block "
                    "(default: true)."
                ),
            },
        },
        "required": ["range", "has_header"],
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        rn = kwargs.get("range") or []
        rn = [rn] if isinstance(rn, str) else (rn or [])
        sort_column = kwargs.get("sort_column", 0)
        ascending = kwargs.get("ascending", True)
        has_header = kwargs.get("has_header", True)

        if len(rn) == 0:
            return self._tool_error("range is required")
        try:
            if len(rn) == 1:
                result = manipulator.sort_range(rn[0], sort_column=sort_column, ascending=ascending, has_header=has_header)
                return {"status": "ok", "message": result}
            for r in rn:
                manipulator.sort_range(r, sort_column=sort_column, ascending=ascending, has_header=has_header)
            return {"status": "ok", "message": f"Sorted {len(rn)} ranges"}
        except Exception as e:
            return self._tool_error(str(e))


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

        try:
            result = manipulator.delete_structure(structure_type, start, count=count)
            return {"status": "ok", "message": result}
        except Exception as e:
            return self._tool_error(str(e))
