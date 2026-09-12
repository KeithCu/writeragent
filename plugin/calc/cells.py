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
from plugin.calc.address_utils import index_to_column, parse_range_string, split_sheet_prefix
from plugin.calc.bridge import CalcBridge
from plugin.calc.base import ToolCalcRangeBase
from plugin.calc.inspector import CellInspector
from plugin.calc.manipulator import CellManipulator

# Verbose per-cell dicts blow chat/tool context (issue 405: A1:H500 → HTTP 400).
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
            "start_column": int(addr.StartColumn),
            "start_row": int(addr.StartRow),
        }
    except Exception:
        log.exception("Could not size range %s for read_cell_range cap; reading in full", range_name)
        return None


def _distinct_display(val: Any) -> str | None:
    """Stable chat label for one getDataArray cell; skip blanks."""
    if val is None or val == "":
        return None
    if isinstance(val, float) and abs(val) < 1e15 and val.is_integer():
        return str(int(val))
    return str(val)


def _column_distinct_peek(data_array: Any, *, start_column: int = 0) -> list[dict[str, Any]]:
    """Cardinality-capped distincts per column from a getDataArray grid.

    First row is the header when it looks like text; high-unique columns
    report a count only (no value list).
    """
    if not isinstance(data_array, (list, tuple)) or not data_array:
        return []
    header_row = data_array[0]
    if not isinstance(header_row, (list, tuple)):
        return []
    n_cols = min(len(header_row), _DISTINCT_PEEK_MAX_COLUMNS)
    if n_cols <= 0:
        return []
    body = data_array[1:]
    out: list[dict[str, Any]] = []
    for col_i in range(n_cols):
        raw_header = header_row[col_i]
        header = raw_header.strip() if isinstance(raw_header, str) and raw_header.strip() else None
        label = header or index_to_column(start_column + col_i)
        # Track every distinct for the count; only the first LIST_CAP go in the payload.
        seen: set[str] = set()
        listed: list[str] = []
        overflow = False
        for row in body:
            if not isinstance(row, (list, tuple)) or col_i >= len(row):
                continue
            key = _distinct_display(row[col_i])
            if key is None or key in seen:
                continue
            seen.add(key)
            if len(listed) < _DISTINCT_PEEK_LIST_CAP:
                listed.append(key)
            if len(seen) > _DISTINCT_PEEK_HIGH_CARDINALITY:
                overflow = True
                break
        entry: dict[str, Any] = {"column": label}
        if overflow:
            entry["unique_count"] = f"{_DISTINCT_PEEK_HIGH_CARDINALITY}+"
            entry["skipped"] = "high cardinality"
        else:
            entry["unique_count"] = len(seen)
            if len(seen) > _DISTINCT_PEEK_LIST_CAP:
                entry["skipped"] = "high cardinality"
            else:
                entry["values"] = listed
        out.append(entry)
    return out


def _try_column_distinct_peek(bridge, range_name: str, preview: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Scan a clipped window of the oversized range via getDataArray. Fail-soft."""
    try:
        start_col = int(preview["start_column"])
        start_row = int(preview["start_row"])
        cols = int(preview["columns"])
        rows = int(preview["rows"])
        end_col = start_col + min(cols, _DISTINCT_PEEK_MAX_COLUMNS) - 1
        end_row = start_row + min(rows, _DISTINCT_PEEK_MAX_ROWS) - 1
        local = f"{index_to_column(start_col)}{start_row + 1}:{index_to_column(end_col)}{end_row + 1}"
        clipped = _format_sheet_address(range_name, local)
        cell_range = bridge.resolve_range_or_address(clipped)
        if not hasattr(cell_range, "getDataArray"):
            return None
        peek = _column_distinct_peek(cell_range.getDataArray(), start_column=start_col)
        return peek or None
    except Exception:
        log.exception("Could not compute column distinct peek for %s", range_name)
        return None


def _leaf_value_count(payload: Any) -> int:
    """Count scalar cells in a JSON array (nested rows flatten)."""
    if not isinstance(payload, list):
        return 1
    n = 0
    for item in payload:
        n += _leaf_value_count(item) if isinstance(item, list) else 1
    return n


def _json_array_value_count(fov: Any) -> int | None:
    """How many cells a JSON/list ``values`` payload names, or None if not an array write.

    Scalar strings fill the whole range; empty / ``[]`` clears. Those are not
    length-checked. Named ranges are handled by the caller (unparseable A1).
    """
    data: Any
    if isinstance(fov, list):
        data = fov
    elif isinstance(fov, str):
        stripped = fov.strip()
        if not stripped.startswith("["):
            return None
        try:
            data = json.loads(stripped)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(data, list):
            return None
    else:
        return None
    if not data:
        return None
    return _leaf_value_count(data)


def _a1_range_shape(range_name: str) -> tuple[int, int, int] | None:
    """``(cell_count, rows, cols)`` for an A1 range, or None for named/invalid refs."""
    try:
        _unused_sheet, local = split_sheet_prefix(range_name)
        (c0, r0), (c1, r1) = parse_range_string(local)
    except Exception:
        # Named ranges and deal.pre misses skip this gate; manipulator still checks 1-D.
        return None
    cols = abs(c1 - c0) + 1
    rows = abs(r1 - r0) + 1
    return cols * rows, rows, cols


def _normalize_source_arg(raw: Any) -> tuple[str | None, str | None]:
    """Parse optional ``source`` (A1 / Sheet.A1). Returns ``(source, error)``."""
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, "source must be a string A1 or Sheet.A1 address (dot sheet prefix, never Excel !)."
    stripped = raw.strip()
    return (stripped or None), None


def _values_conflict_with_source(kwargs: dict[str, Any]) -> bool:
    """True when ``source`` is set and ``values`` was also passed (including empty).

    Keith rule: source and values are mutually exclusive. Empty ``values`` still
    counts as provided so a paste-copy is not confused with a clear/write.
    """
    if "values" not in kwargs:
        return False
    return kwargs.get("values") is not None


def _values_length_mismatch_message(range_name: str, n_vals: int, n_cells: int, rows: int, cols: int) -> str:
    """Loud error so the model can resize the array or the range (no silent zip/pad)."""
    hint = ""
    if cols == 1 and rows > 1:
        hint = " Write one value per row (each formula uses that row's cells)."
    elif rows == 1 and cols > 1:
        hint = " Write one value per column."
    return (
        f"Array has {n_vals} values but range {range_name} has {n_cells} cells "
        f"({rows}×{cols}). JSON array must match range size exactly, or pass a "
        f"single string to fill the whole range.{hint}"
    )


class ReadCellRange(ToolBase):
    """Read values from one or more cell ranges."""

    name = "read_cell_range"
    description = (
        "Reads values from the specified cell range(s). Inspection only — keep ranges small "
        "(headers or a few dozen cells). A large dump overloads chat context; oversized reads "
        "return a peek plus size only. Row-wise transforms use write_formula_range (fill-down); "
        "reductions that spill a small result use =PY into one empty cell outside the data. "
        "Date/time-formatted numeric "
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
        payload: dict[str, Any] = {
            "status": "ok",
            "truncated": True,
            "message": _READ_CELL_RANGE_TRUNCATED_MSG.format(
                rows=preview["rows"], columns=preview["columns"], cells=preview["cells"]
            ),
            "range": range_name,
            "preview_range": preview["preview_range"],
            "rows": preview["rows"],
            "columns": preview["columns"],
            "cells": preview["cells"],
            "result": [grid],
        }
        distincts = _try_column_distinct_peek(bridge, range_name, preview)
        if distincts:
            payload["column_distincts"] = distincts
        return payload


class WriteCellRange(ToolBase):
    """Write formulas or values to a cell range."""

    name = "write_formula_range"
    description = (
        "Writes formulas or values to a cell range(s) efficiently. Single string fills entire range; "
        "JSON array must match range size exactly (one value per cell); or multiline CSV from a start "
        "cell. Use an empty string or empty array to clear contents. Supports lists for non-contiguous "
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
        'Always use data.to_pandas() rather than pd.DataFrame(data) because to_pandas() uses row 0 as column headers; '
        'pd.DataFrame(data) treats headers as data and generates synthetic numeric columns (0..N) that spill as a junk top row. '
        "np.unique on mixed rows fails — NumPy object arrays cannot compare/hash mixed cell types. "
        "DO: to copy a block onto another sheet or place, pass source and dest range; do not pass "
        "values. Dest is the top-left (or a matching range whose start is used); the copied size is "
        "the source extent. Relative formula refs adjust for the dest offset; $ stay."
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
                    "Sheet prefixes target that sheet without switching the active sheet. "
                    "With source, this is the paste top-left (source extent is copied)."
                ),
            },
            # Pin vs fill-down fork lives on values (not CALC_CORE FORMULAS
            # and not the main tool description). #657 replaced the old
            # pin-workaround with fill-down teaching; a JSON array of copied
            # =B2*… still pins every row to the first ref.
            "values": {
                "type": "string",
                "description": (
                    "Required unless source is set (do not pass both). "
                    "Single string: fills the entire range with that value or formula "
                    "(use '=' prefix for formulas). One ordinary formula into a "
                    "multi-cell column or row is fill-down/across — relative A1 refs "
                    "adjust per cell ($ stays absolute). Prefer that for a related "
                    "rate or tax column. In formulas, other sheets are Sheet.A1 "
                    "(dot), not Excel Sheet!A1. JSON array: exact per-cell contents; "
                    "must have exactly as many elements as cells in the range "
                    "(e.g. '[\"a\", \"b\"]' for 2 cells). Repeating the same "
                    "=B2*…-style formula in every element pins every row to the "
                    "first ref — use one formula string over the whole column "
                    "range instead. Empty string/array clears the range. "
                    "To mark rows by criteria, write one "
                    "=IF(OR(<criterion>; …);1;0) into the first data cell of the "
                    "named flag column and fill-down; do not hand-pick rows or "
                    "park the mark in another column. Put a computed result in "
                    "its own cell with a plain label in the adjacent cell — do "
                    "not bury the number in a label string."
                ),
            },
            "source": {
                "type": "string",
                "description": (
                    "Optional source block to copy (A1 or Sheet.A1, dot sheet prefix, "
                    "never Excel Sheet1!A1). When set, dest range is the paste top-left "
                    "(or a matching range whose start is used); copied size is the source "
                    "extent. Do not pass values."
                ),
            },
        },
        "required": ["range"],
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
        source, source_err = _normalize_source_arg(kwargs.get("source"))
        if source_err:
            return self._tool_error(source_err)

        if len(rn) == 0:
            return self._tool_error("range is required")

        # source XOR values: a paste-copy must not also write/clear a values payload.
        if source:
            if _values_conflict_with_source(kwargs):
                return self._tool_error(
                    "Do not pass values when source is set. "
                    "To copy a block, pass source and dest range only."
                )
            undo = WriterCompoundUndo(ctx.doc, "WriterAgent: Copy range")
            try:
                copied: dict[str, Any] | None = None
                for dest in rn:
                    copied = manipulator.copy_formula_range(source, dest)
                if copied is None:
                    return self._tool_error("range is required")
                msg = copied["message"]
                if len(rn) > 1:
                    msg = (
                        f"Copied {copied['rows_copied']}×{copied['cols_copied']} "
                        f"from {source} onto {len(rn)} ranges."
                    )
                return {
                    "status": "ok",
                    "message": msg,
                    "rows_copied": copied["rows_copied"],
                    "cols_copied": copied["cols_copied"],
                }
            except Exception as e:
                return self._tool_error(str(e))
            finally:
                undo.close()

        fov = kwargs.get("values")
        # values is required when source is omitted (schema keeps it optional so
        # source-only calls pass validate). Missing must not fall through as clear.
        if fov is None:
            return self._tool_error("values is required when source is omitted")
        # Normalize: schema is string for Gemini; accept number/list from other providers
        if isinstance(fov, (int, float)):
            fov = str(fov)
        elif isinstance(fov, list):
            fov = json.dumps(fov) if fov else ""

        # Schema already says JSON array length must match the range. Models
        # (solar-pro4) still passed 4 values into an 8-cell range; zip-style
        # writes silently corrupt the extra cells. Fail before undo so the
        # model can resize the array or the range. Named/unparseable A1 is
        # left to the manipulator (it already errors on a 1-D mismatch).
        n_vals = _json_array_value_count(fov)
        if n_vals is not None:
            for r in rn:
                shape = _a1_range_shape(r)
                if shape is None:
                    continue
                n_cells, rows, cols = shape
                if n_vals != n_cells:
                    return self._tool_error(
                        _values_length_mismatch_message(r, n_vals, n_cells, rows, cols)
                    )

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
    # Action-time key/direction and "use this tool, don't rewrite the
    # block" live here. Do not restack another SORT Don't/Do into
    # CALC_CORE (flash merges adjacent identical shapes).
    description = (
        "Stable one-column sort of the specified range(s) by values in one column. "
        "Multi-key sorts are multiple calls (two stable one-column passes). "
        "Do call sort_range to reorder rows (not rewrite the block with "
        "write_formula_range) because hand-written order often leaves labels mid-table. "
        "Pick sort_column for the metric to order by (0-based within the range); "
        "set ascending=false when largest values should come first. "
        "Do pass has_header=true when row 1 is labels because otherwise labels "
        "sort as values. "
        "Supports lists for non-contiguous areas."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range": {"type": "array", "items": {"type": "string"}, "description": ('Range(s) to sort (e.g. ["A1:D10"] or ["A1:B10", "D1:E10"]).')},
            "sort_column": {
                "type": "integer",
                "description": (
                    "0-based index of the key column inside the range "
                    "(0 = leftmost; default: 0). Use the numeric/metric column, "
                    "not the label column, when sorting by amount."
                ),
            },
            "ascending": {
                "type": "boolean",
                "description": (
                    "true = smallest first (default); false = largest/highest first."
                ),
            },
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
