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
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, ClassVar
from plugin.framework.errors import ToolExecutionError, WriterAgentException, format_error_payload, is_disposal
from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.calc.address_utils import format_range, index_to_column, parse_address, quote_sheet_name, split_sheet_prefix
from plugin.calc.bridge import CalcBridge, is_agent_visible_sheet
from plugin.calc.base import ToolCalcRangeBase
from plugin.calc.calc_utils import range_args
from plugin.calc.inspector import CellInspector, grid_to_rows
from plugin.calc.manipulator import CellManipulator
from plugin.doc.compound_undo import UndoStepOrRollback
from plugin.doc.visual_helpers import parse_color_to_uno_int

# Verbose per-cell dicts for a large range (A1:H500) exceed the chat context and the provider rejects the request (HTTP 400).
# Inspector.read_range stays uncapped for UNO tests and internal callers.
_READ_CELL_RANGE_MAX_CELLS = 80
_READ_CELL_RANGE_PREVIEW_ROWS = 10
# Size + peek + fill-down first; =PY only for small reductions (not a hard =PY funnel).
_READ_CELL_RANGE_TRUNCATED_MSG = (
    "Range is too large to load into chat (would overload the model context): "
    "{rows} rows × {columns} columns ({cells} cells). "
    "The sample below is a peek only — do not re-read the full range. "
    "Row-wise ordinary Calc formulas: write_formula_range into a 1-column "
    "(or 1-row) destination; fill-down adjusts relative refs. "
    "Reductions that spill a small result: write =PY into one empty cell "
    "outside the data."
)
# Truncated-path distinct peek only (not a public tool). High-unique columns
# are summarized, not listed — no phone book in chat.
_DISTINCT_PEEK_MAX_COLUMNS = 8
_DISTINCT_PEEK_MAX_ROWS = 2000
_DISTINCT_PEEK_LIST_CAP = 12
_DISTINCT_PEEK_HIGH_CARDINALITY = 40

log = logging.getLogger(__name__)


def _parse_structure_start(
    structure_type: str, start_raw: Any
) -> tuple[int | str | None, str | None, str | None]:
    """Validate structure start; optional sheet prefix from a qualified string.

    Strict start validation: non-ASCII digits, floats, bools, and cell addresses rejected.

    Returns (start, sheet_prefix, error_message).
    """
    sheet_prefix: str | None = None
    local_raw = start_raw

    if isinstance(start_raw, str):
        sheet_prefix, local = split_sheet_prefix(start_raw.strip())
        local_raw = local

    if structure_type == "rows":
        if type(local_raw) is int:
            row_val = local_raw
        elif isinstance(local_raw, str) and local_raw.isascii() and local_raw.isdigit():
            row_val = int(local_raw)
        else:
            return None, sheet_prefix, 'start for rows must be a 1-based row number (for example 5 or "5")'
        if row_val < 1:
            return None, sheet_prefix, 'start for rows must be a 1-based row number (for example 5 or "5")'
        return row_val, sheet_prefix, None

    if isinstance(local_raw, str) and local_raw.isascii() and local_raw.isalpha():
        return local_raw.upper(), sheet_prefix, None
    return None, sheet_prefix, 'start for columns must be a column letter (for example "C")'


def _opt_color(kwargs: dict[str, Any], key: str) -> tuple[int | None, str | None]:
    raw = kwargs.get(key)
    if raw is None or raw.strip() == "":
        return None, None
    parsed = parse_color_to_uno_int(raw)
    if parsed is None:
        return None, f"Invalid {key}: '{raw}'"
    return parsed, None


def _sheet_prefix_for_clip(range_name: str, resolved: Any) -> str:
    """Prefix including the separator, or '' when the clip stays unqualified.

    A prefix already on range_name wins. split_sheet_prefix turns 'O''Brien'
    into O'Brien; quote and double again on the way out. Keep '!' when the
    input used a bang, otherwise '.'. An unqualified name (a named range)
    takes the sheet from resolved.getSpreadsheet().getName() and always
    uses '.'.
    """
    sheet, _unused_local = split_sheet_prefix(range_name)
    if sheet:
        sep = "!" if "!" in range_name else "."
        if range_name.lstrip().startswith("'") or not sheet.isidentifier():
            token = "'" + sheet.replace("'", "''") + "'"
        else:
            token = sheet
        return f"{token}{sep}"

    if hasattr(resolved, "getSpreadsheet"):
        sp = resolved.getSpreadsheet()
        name = sp.getName()
        if isinstance(name, str) and name:
            return quote_sheet_name(name) + "."
    return ""


def _manipulator_context(ctx: ToolContext, range_arg: Any) -> tuple[CellManipulator, list[Any]]:
    return CellManipulator(CalcBridge(ctx.doc)), range_args(range_arg)


def _mutation_error(tool: ToolBase, exc: Exception, message: str) -> dict[str, Any]:
    """Build a tool error dict that preserves WriterAgentException code and details.

    A WriterAgentException's code and details (for example the sheet list of a
    missing sheet) must reach the agent instead of being flattened to a generic
    tool error. Plain exceptions keep the tool's default error code. Note that
    ``exc.details`` is unpacked into ``_tool_error`` kwargs, so keys named
    ``suggested_call`` or ``top_level`` would be routed to those parameters.
    """
    if isinstance(exc, WriterAgentException):
        return tool._tool_error(message, code=exc.code, **exc.details)
    return tool._tool_error(message)


def _mutate_ranges(
    tool: ToolBase,
    doc: Any,
    ranges: list[Any],
    undo_title: str,
    apply_one: Callable[[Any], str | dict[str, Any]],
    done_message: str = "",
    format_result: Callable[[list[Any]], dict[str, Any]] | None = None,
    plan_one: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Run apply_one on each range inside one compound undo."""
    if len(ranges) == 0:
        return tool._tool_error("range is required", code="INVALID_PARAM")

    target = ranges[0]
    results: list[Any] = []
    group: UndoStepOrRollback | None = None
    try:
        # Plan every range before the undo group exists (UndoStepOrRollback enters its context in
        # __init__): a bad value shape, unknown range or occupied array target on any range is
        # refused before the first range changes, so there is nothing to undo and no Redo entry.
        steps: list[Any] = list(ranges)
        if plan_one is not None:
            steps = []
            for r in ranges:
                target = r
                steps.append(plan_one(r))
        group = UndoStepOrRollback(doc, undo_title)
        with group:
            for r, step in zip(ranges, steps):
                target = r
                res = apply_one(step)
                results.append(res)
        if format_result is not None:
            return format_result(results)
        if len(ranges) == 1:
            single = results[0]
            if isinstance(single, dict):
                return {"status": "ok", **single}
            return {"status": "ok", "message": single}
        return {"status": "ok", "message": done_message}
    except Exception as exc:
        if is_disposal(exc):
            raise
        if group is not None and group.rolled_back:
            return _mutation_error(tool, exc, f"Failed on range {target!r}: {exc}. Nothing was kept.")
        entered = group is not None and group.entered
        if len(results) > 0:
            if not entered:
                return _mutation_error(tool, exc, f"Failed on range {target!r}: {exc}. {len(results)} range(s) were already changed individually.")
            return _mutation_error(tool, exc, f"Failed on range {target!r}: {exc}. {len(results)} range(s) were already changed; one Ctrl+Z undoes them.")
        return _mutation_error(tool, exc, str(exc))


def _preview_if_large(bridge: Any, range_name: str) -> dict[str, Any] | None:
    """Return clip metadata when the range is too big for a full chat dump, else None."""
    try:
        cell_range = bridge.resolve_range_or_address(range_name)
        if not hasattr(cell_range, "getRangeAddress"):
            return None
        addr = cell_range.getRangeAddress()
        sc, sr = int(addr.StartColumn), int(addr.StartRow)
        ec, er = int(addr.EndColumn), int(addr.EndRow)
        rows, cols = er - sr + 1, ec - sc + 1
        cells = rows * cols
        if cells <= _READ_CELL_RANGE_MAX_CELLS:
            return None
        preview_rows = min(rows, _READ_CELL_RANGE_PREVIEW_ROWS)
        preview_cols = min(cols, _DISTINCT_PEEK_MAX_COLUMNS)
        local = format_range(sc, sr, sc + preview_cols - 1, sr + preview_rows - 1)

        prefix = _sheet_prefix_for_clip(range_name, cell_range)
        preview_range = f"{prefix}{local}"

        return {
            "rows": rows,
            "columns": cols,
            "cells": cells,
            "preview_range": preview_range,
            "sheet_prefix": prefix,
            "start_column": sc,
            "start_row": sr,
        }
    except Exception as e:
        if is_disposal(e) or isinstance(e, WriterAgentException):
            raise
        raise ToolExecutionError(f"Could not size range {range_name}: {e}")


def _distinct_display(val: Any) -> str | None:
    """Stable chat label for one getDataArray cell; skip blanks."""
    if val is None or val == "":
        return None
    if isinstance(val, float) and abs(val) < 1e15 and val.is_integer():
        return str(int(val))
    return str(val)


def _column_distinct_peek(data_array: Any, *, start_column: int = 0) -> list[dict[str, Any]]:
    """Cardinality-capped distincts per column from a getDataArray grid.

    Columns whose first cell is text treat that cell as a header label and scan
    subsequent rows; numeric or blank first cells are counted as data including row 0.
    """
    # Per-column row 0 decides header vs data so numeric or empty headers do not hide distinct values.
    if not data_array:
        return []
    header_row = data_array[0]
    n_cols = min(len(header_row), _DISTINCT_PEEK_MAX_COLUMNS)
    out: list[dict[str, Any]] = []
    for col_i in range(n_cols):
        raw_header = header_row[col_i]
        if isinstance(raw_header, str) and raw_header.strip():
            label = raw_header.strip()
            scan_rows = data_array[1:]
        else:
            label = index_to_column(start_column + col_i)
            scan_rows = data_array
        # Track every distinct for the count; only the first LIST_CAP go in the payload.
        seen: dict[str, None] = {}
        for row in scan_rows:
            if col_i >= len(row):
                continue
            key = _distinct_display(row[col_i])
            if key is None or key in seen:
                continue
            seen[key] = None
            if len(seen) > _DISTINCT_PEEK_HIGH_CARDINALITY:
                break
        entry: dict[str, Any] = {"column": label}
        if len(seen) > _DISTINCT_PEEK_HIGH_CARDINALITY:
            entry["unique_count"] = f"{_DISTINCT_PEEK_HIGH_CARDINALITY}+"
        else:
            entry["unique_count"] = len(seen)
        if len(seen) > _DISTINCT_PEEK_LIST_CAP:
            entry["skipped"] = "high cardinality"
        else:
            entry["values"] = list(seen)
        out.append(entry)
    return out


def _try_column_distinct_peek(bridge: Any, range_name: str, preview: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Scan a clipped window of the oversized range via getDataArray. Fail-soft."""
    try:
        start_col = preview["start_column"]
        start_row = preview["start_row"]
        cols = preview["columns"]
        rows = preview["rows"]
        end_col = start_col + min(cols, _DISTINCT_PEEK_MAX_COLUMNS) - 1
        end_row = start_row + min(rows, _DISTINCT_PEEK_MAX_ROWS) - 1
        local = format_range(start_col, start_row, end_col, end_row)
        clipped = f"{preview['sheet_prefix']}{local}"
        cell_range = bridge.resolve_range_or_address(clipped)
        if not hasattr(cell_range, "getDataArray"):
            return None
        peek = _column_distinct_peek(cell_range.getDataArray(), start_column=start_col)
        if not peek:
            return None
        if rows > _DISTINCT_PEEK_MAX_ROWS:
            for entry in peek:
                entry["scanned_rows"] = _DISTINCT_PEEK_MAX_ROWS
        return peek
    except Exception as e:
        if is_disposal(e):
            raise
        log.exception("Could not compute column distinct peek for %s", range_name)
        return None


class ReadCellRange(ToolBase):
    """Read values from one or more cell ranges."""

    name: str | None = "read_cell_range"
    description: str = (
        "Reads values from the specified cell range(s). Inspection only — keep ranges small "
        "(headers or a few dozen cells). A large dump overloads chat context; oversized reads "
        "return a peek plus size only. Row-wise transforms use write_formula_range (fill-down); "
        "reductions that spill a small result use =PY into one empty cell outside the data. "
        "Default `format=rows` returns a 2D `rows` array of cell values (ISO 8601 dates/times, "
        "`PTnHnMnS` durations, formula errors as display strings) plus sparse `formulas` and "
        "`types` maps only where needed. Use `format=cells` for one object per cell "
        "(address, value, formula, type, format_category, format_code). "
        "Supports lists for non-contiguous areas. `result` has one entry per requested range, in order, even for one range: `{status, range, result: [payload]}`, where payload is the `rows` object or the `cells` grid; an oversized range's entry adds `truncated`, `message`, `hint`, `preview_range`, `row_count`, `column_count`, `cell_count` and optional `column_distincts`."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "range": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    'Cell range(s) (e.g. ["A1:D10"], ["Sheet1.A1:C5"], ["\'Data Sheet\'!B2"]). '
                    "Sheet prefixes target that sheet without switching the active sheet."
                ),
            },
            "format": {
                "type": "string",
                "enum": ["rows", "cells"],
                "default": "rows",
                "description": (
                    "rows (default): 2D values, formulas and date/time types only where present. "
                    "cells: one object per cell with address, value, formula, type, "
                    "format_category, and format_code."
                ),
            },
        },
        "required": ["range"],
    }
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier: str = "core"
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        wire_format: str = kwargs.get("format", "rows")

        bridge = CalcBridge(ctx.doc)
        inspector = CellInspector(bridge)
        rn = range_args(kwargs["range"])

        if len(rn) == 0:
            return self._tool_error("range is required", code="INVALID_PARAM")
        results = [self._read_one(bridge, inspector, r, wire_format) for r in rn]
        failed = [item["range"] for item in results if item.get("status") != "ok"]
        if failed:
            # One range answers with that range's own code, message and details, so a missing sheet still reaches the agent as UNO_OBJECT_ERROR with the sheet names.
            if len(rn) == 1:
                return results[0]
            return {
                "status": "error",
                "message": f"Failed to read {len(failed)} of {len(rn)} ranges: {failed!r}",
                "result": results,
            }
        return {"status": "ok", "result": results}

    def _wire_result(
        self,
        grid: list[list[dict[str, Any]]],
        wire_format: str,
        range_name: str,
    ) -> list[Any]:
        if wire_format == "cells":
            return [grid]
        return [grid_to_rows(grid, range_name=range_name)]

    def _read_one(self, bridge: Any, inspector: Any, range_name: str, wire_format: str) -> dict[str, Any]:
        """Read one range; preview-only when the full grid would swamp chat context."""
        try:
            preview = _preview_if_large(bridge, range_name)
            if preview is None:
                grid = inspector.read_range(range_name, include_format_info=True)
                return {"status": "ok", "result": self._wire_result(grid, wire_format, range_name), "range": range_name}
            grid = inspector.read_range(preview["preview_range"], include_format_info=True)
            # Size metadata uses row_count/column_count/cell_count so it cannot collide with result[0]["rows"] (2D values).
            payload: dict[str, Any] = {
                "status": "ok",
                "truncated": True,
                "message": _READ_CELL_RANGE_TRUNCATED_MSG.format(rows=preview["rows"], columns=preview["columns"], cells=preview["cells"]),
                "range": range_name,
                "preview_range": preview["preview_range"],
                "row_count": preview["rows"],
                "column_count": preview["columns"],
                "cell_count": preview["cells"],
                "result": self._wire_result(grid, wire_format, range_name),
            }
            distincts = _try_column_distinct_peek(bridge, range_name, preview)
            if distincts:
                payload["column_distincts"] = distincts
            payload["hint"] = (
                "Peek only (%s). Row-wise formulas: write_formula_range on a 1-column or 1-row range. "
                "A small reduction: =PY in one empty cell outside the data."
                % preview["preview_range"]
            )
            return payload
        except Exception as exc:
            if is_disposal(exc):
                raise
            if isinstance(exc, WriterAgentException):
                # Keep the per-range key alongside the bridge's error code and details.
                return {**format_error_payload(exc), "range": range_name}
            log.exception("Failed to read range %s", range_name)
            return {"status": "error", "range": range_name, "message": str(exc) or type(exc).__name__}


class WriteCellRange(ToolBase):
    """Write formulas or values to a cell range."""

    name: str | None = "write_formula_range"
    description: str = (
        "Writes formulas or values to a cell range(s) efficiently. Single string fills entire range; "
        "a 2-D array (one inner array per row) must match the range; or multiline CSV from a start "
        "cell. A JSON string of an array is still accepted. Use an empty string or empty array to clear contents. "
        "Supports lists for non-contiguous "
        "areas. A single ordinary Calc formula into a 1-column or 1-row range fill-down/across "
        "adjusts relative A1 refs ($ stays absolute). Prefer plain values/ISO "
        "dates for static cells; use an '=' formula only when the cell must stay live (e.g. TODAY(), "
        "computed duration). Dates and times: use ISO 8601 only — YYYY-MM-DD, HH:MM[:SS], or "
        "YYYY-MM-DDTHH:MM[:SS]. These become real Calc date/time values. Elapsed/stopwatch values: "
        "use PTnHnMnS (e.g. PT30H, PT1H30M); these become duration serials with elapsed formatting. "
        "Do not include a timezone offset or Z, and do not use locale forms like 08/05/2026; those "
        "are stored as text. Prefix with an apostrophe ('2026-08-08) to force text. "
        'Reductions that spill a small result: write =PY("result = …"; DataRange) into one empty cell '
        "outside DataRange (e.g. J1 for A1:H500, or a new sheet). That cell spills the 2D result "
        "(values are in the neighbors). A small peek of the origin or headers is enough — do not "
        "dump the input or full spill into chat; do not write =PY onto DataRange (circular). If "
        "they asked for in-place unique rows, still land beside/new sheet and say where. "
        'Tables (headers, mixed types): =PY("result = data.to_pandas().drop_duplicates()"; DataRange). '
        "Always use data.to_pandas() rather than pd.DataFrame(data) because to_pandas() uses row 0 as column headers; "
        "pd.DataFrame(data) treats headers as data and generates synthetic numeric columns (0..N) that spill as a junk top row. "
        "np.unique on mixed rows fails — NumPy object arrays cannot compare/hash mixed cell types. "
        "DO: to copy a block onto another sheet or place, pass source and dest range; do not pass "
        "values. Dest is the top-left (or a matching range whose start is used); the copied size is "
        "the source extent. Relative formula refs adjust for the dest offset; $ stay."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "range": {
                "type": "array",
                "items": {"type": "string"},
                "description": ('Target range(s) (e.g. ["A1:A10"], ["Sheet1.B2:D2"]). Use a dot for other sheets (Sheet1.B2), never Excel Sheet1!B2. Sheet prefixes target that sheet without switching the active sheet. With source, this is the paste top-left (source extent is copied).'),
            },
            # The fill-down rule lives on the values description (not CALC_CORE
            # FORMULAS and not the main tool description): one formula string fills
            # the range with shifted refs, while a JSON array of copied =B2*… writes
            # each string as given, so every row keeps the first ref.
            # string|array|null on chat + MCP: _normalize_schema_for_strict_providers
            # keeps real unions (same pattern as apply_document_content content).
            "values": {
                "type": ["string", "array", "null"],
                "items": {
                    "type": ["array", "string", "number", "boolean", "null"],
                    "items": {"type": ["string", "number", "boolean", "null"]},
                },
                "description": (
                    "Required unless source is set (do not pass both). "
                    "Single string: fills the entire range with that value or formula "
                    "(use '=' prefix for formulas). One ordinary formula into a "
                    "multi-cell column or row is fill-down/across — relative A1 refs "
                    "adjust per cell ($ stays absolute). Prefer that for a related "
                    "rate or tax column. In formulas, other sheets are Sheet.A1 "
                    "(dot), not Excel Sheet!A1. 2-D array: one inner array per row; "
                    "must match the range. Cells are string, number, boolean, or null "
                    '(e.g. [["a", 1], ["b", null]] for A1:B2). Repeating the same '
                    "=B2*…-style formula in every cell pins every row to the "
                    "first ref — use one formula string over the whole column "
                    "range instead. A JSON string of a flat or 2-D array is still "
                    "accepted. Empty string or empty array clears. "
                    "A top-level FILTER/SORT/UNIQUE (or SEQUENCE and other "
                    "array functions) is entered as an array formula over its "
                    "result; occupied cells besides the origin are refused. "
                    "=PY() is still the spill path for Python reductions."
                ),
            },
            "array": {"type": "boolean", "description": ("Optional. true forces an array formula even for LET/XLOOKUP; false forces a scalar formula (setFormula / fill-down).")},
            "source": {"type": "string", "description": ("Optional source block to copy (A1 or Sheet.A1, dot sheet prefix, never Excel Sheet1!A1). When set, dest range is the paste top-left (or a matching range whose start is used); copied size is the source extent. Do not pass values.")},
        },
        "required": ["range"],
    }
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier: str = "core"
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        manipulator, rn = _manipulator_context(ctx, kwargs["range"])
        source = (kwargs.get("source") or "").strip() or None

        if len(rn) == 0:
            return self._tool_error("range is required", code="INVALID_PARAM")

        # source XOR values: a paste-copy must not also write/clear a values payload.
        # Explicit values, including "", conflict; a missing key and None do not.
        if source:
            if kwargs.get("values") is not None:
                return self._tool_error(
                    "Do not pass values when source is set. To copy a block, pass source and dest range only.",
                    code="INVALID_PARAM",
                )

            def _format_copy(results: list[Any]) -> dict[str, Any]:
                last = results[-1]
                msg = last["message"]
                if len(rn) > 1:
                    msg = f"Copied {last['rows_copied']}×{last['cols_copied']} from {source} onto {len(rn)} ranges."
                return {
                    "status": "ok",
                    "message": msg,
                    "rows_copied": last["rows_copied"],
                    "cols_copied": last["cols_copied"],
                }

            return _mutate_ranges(
                self,
                ctx.doc,
                rn,
                "WriterAgent: Copy range",
                lambda dest: manipulator.copy_formula_range(source, dest),
                format_result=_format_copy,
            )

        fov = kwargs.get("values")
        # values is required when source is omitted (schema keeps it optional so
        # source-only calls pass validate). Missing must not fall through as clear.
        if fov is None:
            return self._tool_error("values is required when source is omitted", code="INVALID_PARAM")

        array_flag: bool | None = kwargs.get("array")

        # The first array formula's measured size is reused for the other ranges: the same
        # formula gives the same result size, and each measure is a UNO probe.
        measured: list[tuple[int, int]] = []

        def _plan(r: Any) -> Any:
            plan = manipulator.plan_write(
                r,
                fov,
                array=array_flag,
                check_cell_types=True,
                premeasured_array_size=measured[0] if measured else None,
            )
            if plan.array_size is not None and not measured:
                measured.append(plan.array_size)
            return plan

        def _format_write(results: list[Any]) -> dict[str, Any]:
            message = f"Wrote to {len(rn)} ranges"
            if any("array_range" in res for res in results):
                ranges_payload = [{"range": r, **res} for r, res in zip(rn, results)]
                return {"status": "ok", "message": message, "ranges": ranges_payload}
            return {"status": "ok", "message": message}

        return _mutate_ranges(
            self,
            ctx.doc,
            rn,
            "WriterAgent: Write formulas",
            manipulator.commit_write,
            format_result=_format_write if len(rn) > 1 else None,
            plan_one=_plan,
        )


class InsertCellHtml(ToolBase):
    """Insert HTML as rich text into a single cell (active sheet)."""

    name: str | None = "insert_cell_html"
    description: str = (
        "Parses HTML with the same filter as Writer and pastes rich text into one cell "
        "(e.g. <b>, <i>, <a href>, line breaks). A sheet prefix (Sheet2.A1, 'Data Sheet'.B2) "
        "selects that sheet; otherwise the active sheet. Does not support images or embedded objects. "
        "Clears existing cell text. Use set_style for table-wide borders."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "cell": {
                "type": "string",
                "description": 'Single cell (e.g. "A1" or "Sheet2.A1").',
            },
            "html": {"type": "string", "description": "HTML fragment or small document (UTF-8)."},
        },
        "required": ["cell", "html"],
    }
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        # Lazy: rich_html imports plugin.writer.html_import, which loads the Writer import and chat stack (about 180 modules); only this tool needs it.
        from plugin.calc.rich_html import insert_cell_html_rich

        addr = kwargs["cell"].strip()
        html = kwargs["html"]
        assert isinstance(html, str)
        if not addr:
            return self._tool_error("cell is required", code="INVALID_PARAM")
        # Cell addresses use the bridge so sheet prefixes resolve (same as other cell tools).
        _prefix, local = split_sheet_prefix(addr)
        try:
            parse_address(local)
        except ValueError as e:
            return self._tool_error(f"Invalid cell address: {e}", code="INVALID_PARAM")

        insert_cell_html_rich(ctx.doc, ctx.ctx, addr, html)
        return {"status": "ok", "message": f"Inserted rich HTML into cell {addr.upper()}."}


class SetCellStyle(ToolBase):
    """Apply style and formatting to cells or ranges."""

    name: str | None = "set_style"
    description: str = "Applies style and formatting to the specified cell(s) or range(s). Supports lists for non-contiguous areas."
    parameters: dict[str, Any] | None = {
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
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation: bool | None = True
    # Kept for scripting API / in-process callers; omitted from LLM schema so models cannot
    # casually rewrite NumberFormat via set_style (see docs/calc/date-time-handling.md S26).
    scripting_only_parameters: frozenset[str] = frozenset({"number_format"})

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        manipulator, rn = _manipulator_context(ctx, kwargs["range"])

        style_kwargs: dict[str, Any] = {}
        for key in ("bg_color", "font_color", "border_color"):
            color_val, err = _opt_color(kwargs, key)
            if err:
                return self._tool_error(err, code="INVALID_PARAM")
            style_kwargs[key] = color_val

        for key in ("bold", "italic", "wrap_text", "h_align", "v_align"):
            style_kwargs[key] = kwargs.get(key)

        font_size = kwargs.get("font_size")
        style_kwargs["font_size"] = None if font_size is None else float(font_size)

        # number_format is scripting-only and has no schema entry, so the validator never type-checks it.
        raw_fmt = kwargs.get("number_format")
        if raw_fmt is not None and not isinstance(raw_fmt, str):
            return self._tool_error(f"number_format must be a string, got {type(raw_fmt).__name__}", code="INVALID_PARAM")
        style_kwargs["number_format"] = raw_fmt if raw_fmt and raw_fmt.strip() else None

        if len(rn) == 0:
            return self._tool_error("range is required", code="INVALID_PARAM")

        # Reject style calls where every property is None—empty mutations create useless undo steps.
        if all(v is None for v in style_kwargs.values()):
            return self._tool_error("No style properties were provided.", code="INVALID_PARAM")

        def _apply_style(r: Any) -> str:
            manipulator.set_cell_style(r, **style_kwargs)
            return f"Style applied to {r}"

        return _mutate_ranges(
            self,
            ctx.doc,
            rn,
            "WriterAgent: Set style",
            _apply_style,
            f"Style applied to {len(rn)} ranges",
        )


class MergeCells(ToolBase):
    """Merge a cell range."""

    name: str | None = "merge_cells"
    description: str = "Merges the specified cell range(s). Typically used for main headers. Write text with write_formula_range and style with set_style after merging. Supports lists for non-contiguous areas."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "range": {"type": "array", "items": {"type": "string"}, "description": ('Range(s) to merge (e.g. ["A1:D1"] or ["A1:B1", "C1:D1"]).')},
            "center": {"type": "boolean", "description": "Center content (default: true)"},
            "unmerge": {"type": "boolean", "description": "Split merged cells instead of merging (default: false)"},
        },
        "required": ["range"],
    }
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        manipulator, rn = _manipulator_context(ctx, kwargs["range"])
        center: bool = kwargs.get("center", True)
        unmerge: bool = kwargs.get("unmerge", False)

        def _apply_merge(r: Any) -> str:
            manipulator.merge_cells(r, center=center, unmerge=unmerge)
            if unmerge:
                return f"Unmerged cells {r}"
            return f"Merged cells {r}"

        return _mutate_ranges(
            self,
            ctx.doc,
            rn,
            "WriterAgent: Merge cells",
            _apply_merge,
            f"Merged cells in {len(rn)} ranges",
        )


class SortRange(ToolCalcRangeBase):
    """Sort a range by a column."""

    name: str | None = "range_sort"
    # Action-time key/direction and "use this tool, don't rewrite the
    # block" live here. Do not restack another SORT Don't/Do into
    # CALC_CORE (flash merges adjacent identical shapes).
    description: str = (
        "Stable one-column sort of the specified range(s) by values in one column. "
        "Multi-key sorts are multiple calls (two stable one-column passes). "
        "Do call range_sort to reorder rows (not rewrite the block with "
        "write_formula_range) because hand-written order often leaves labels mid-table. "
        "Pick sort_column for the metric to order by (0-based within the range); "
        "set ascending=false when largest values should come first. "
        "Do pass has_header=true when row 1 is labels because otherwise labels "
        "sort as values. "
        "Supports lists for non-contiguous areas. "
        "The result says 'no rows moved' when the key column was already in that order."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "range": {"type": "array", "items": {"type": "string"}, "description": ('Range(s) to sort (e.g. ["A1:D10"] or ["A1:B10", "D1:E10"]).')},
            "sort_column": {"type": "integer", "description": ("0-based index of the key column inside the range (0 = leftmost; default: 0). Use the numeric/metric column, not the label column, when sorting by amount.")},
            "ascending": {"type": "boolean", "description": ("true = smallest first (default); false = largest/highest first.")},
            "has_header": {"type": "boolean", "description": ("true when row 1 is labels; false only for a headerless block (default: true).")},
        },
        "required": ["range", "has_header"],
    }
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        manipulator, rn = _manipulator_context(ctx, kwargs["range"])

        sort_column: int = kwargs.get("sort_column", 0)
        # The schema makes sort_column an integer but does not read minimum; refuse negatives here as INVALID_PARAM before any undo group. The width bound lives in range_sort.
        if sort_column < 0:
            return self._tool_error(f"sort_column must be an integer >= 0, got {sort_column}", code="INVALID_PARAM")

        ascending: bool = kwargs.get("ascending", True)

        has_header: bool = kwargs["has_header"]
        assert isinstance(has_header, bool)

        return _mutate_ranges(
            self,
            ctx.doc,
            rn,
            "WriterAgent: Sort range",
            lambda r: manipulator.range_sort(r, sort_column=sort_column, ascending=ascending, has_header=has_header),
            format_result=(lambda results: {"status": "ok", "message": " ".join(results)}) if len(rn) > 1 else None,
        )


class _StructureTool(ToolBase):
    """Base for row/column insert and delete tools.

    Both tools share argument parsing and the error rule; ``insert`` picks the manipulator verb.
    """

    insert: ClassVar[bool]
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        structure_type: str = kwargs["structure_type"]
        assert structure_type in ("rows", "columns")
        # The schema makes count an integer; it does not read a minimum, so 0 and negatives stop here.
        count: int = kwargs.get("count", 1)
        if count < 1:
            return self._tool_error(f"count must be an integer >= 1, got {count}", code="INVALID_PARAM")

        start, sheet_prefix, start_err = _parse_structure_start(structure_type, kwargs["start"])
        if start_err:
            return self._tool_error(start_err, code="INVALID_PARAM")

        if sheet_prefix is not None and not is_agent_visible_sheet(sheet_prefix):
            return self._tool_error(f"Cannot modify internal sheet '{sheet_prefix}'.", code="INVALID_PARAM")

        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        sheet = bridge.get_sheet(sheet_prefix) if sheet_prefix is not None else None
        mutate = manipulator.insert_structure if self.insert else manipulator.delete_structure
        try:
            assert start is not None
            return {"status": "ok", "message": mutate(structure_type, start, count=count, sheet=sheet)}
        except Exception as e:
            # Disposal and UnoObjectError must re-raise so execute_safe keeps the right code.
            if is_disposal(e) or isinstance(e, WriterAgentException):
                raise
            return self._tool_error(str(e))


class DeleteStructure(_StructureTool):
    """Delete rows or columns."""

    insert: ClassVar[bool] = False
    name: str | None = "delete_structure"
    description: str = "Deletes rows or columns. Use for structural changes; prefer ranges for data operations."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "structure_type": {"type": "string", "enum": ["rows", "columns"], "description": "Type of structure to delete."},
            "start": {"type": ["integer", "string"], "description": ('For rows: 1-based row number (e.g. 5); for columns: column letter (e.g. "C").')},
            "count": {"type": "integer", "description": "Number to delete (default 1)."},
        },
        "required": ["structure_type", "start"],
    }


class InsertStructure(_StructureTool):
    """Insert rows or columns."""

    insert: ClassVar[bool] = True
    name: str | None = "insert_structure"
    description: str = "Inserts empty rows or columns. Use for structural changes; prefer ranges for data operations."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "structure_type": {"type": "string", "enum": ["rows", "columns"], "description": "Type of structure to insert."},
            "start": {"type": ["integer", "string"], "description": ('For rows: 1-based row number (e.g. 5); for columns: column letter (e.g. "C"). Sheet prefix allowed (e.g. Sheet2.5).')},
            "count": {"type": "integer", "description": "Number to insert (default 1)."},
        },
        "required": ["structure_type", "start"],
    }
