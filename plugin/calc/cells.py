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
from typing import TYPE_CHECKING, Any, Callable

from plugin.framework.errors import ToolExecutionError, is_disposed_exception, is_real_disposal, uno_probe
from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.calc.address_utils import format_range, index_to_column, parse_range_string, quote_sheet_name, split_sheet_prefix
from plugin.calc.bridge import CalcBridge
from plugin.calc.base import ToolCalcRangeBase
from plugin.calc.inspector import CellInspector
from plugin.calc.manipulator import CellManipulator, _parse_formula_or_values_string, _values_length_mismatch_message
from plugin.doc.visual_helpers import parse_color_to_uno_int
from plugin.framework.deal_shim import deal

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

log = logging.getLogger("writeragent.calc")


def _opt_bool(kwargs: dict[str, Any], key: str) -> tuple[bool | None, str | None]:
    if key not in kwargs:
        return None, None
    raw = kwargs[key]
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return raw, None
    if isinstance(raw, str):
        cleaned = raw.strip().lower()
        if cleaned in ("true", "false"):
            return cleaned == "true", None
    return None, f"{key} must be a boolean (true or false), got {type(raw).__name__}"


def _strict_int(kwargs: dict[str, Any], key: str, minimum: int | None = None) -> tuple[int | None, str | None]:
    if key not in kwargs:
        return None, None
    raw = kwargs[key]
    if raw is None:
        return None, None
    if type(raw) is not int:
        msg = f"{key} must be an integer"
        if minimum is not None:
            msg += f" >= {minimum}"
        return None, f"{msg}, got {type(raw).__name__}"
    if minimum is not None and raw < minimum:
        return None, f"{key} must be an integer >= {minimum}, got {raw}"
    return raw, None


def _opt_color(kwargs: dict[str, Any], key: str) -> tuple[int | None, str | None]:
    if key not in kwargs:
        return None, None
    raw = kwargs[key]
    if raw is None:
        return None, None
    if isinstance(raw, str) and raw.strip() == "":
        return None, None
    if not isinstance(raw, str):
        return None, f"{key} must be a string, got {type(raw).__name__}"
    parsed = _parse_color(raw)
    if parsed is None:
        return None, f"Invalid {key}: '{raw}'"
    return parsed, None


# ── Colour helper ──────────────────────────────────────────────────────


@deal.post(lambda result: result is None or (isinstance(result, int) and 0 <= result <= 0xFFFFFF))
def _parse_color(color_str: Any) -> int | None:
    """Convert a hex colour string or named colour to an RGB integer.

    String-only wrapper around :func:`parse_color_to_uno_int` so Calc ``set_style``
    keeps rejecting non-string LLM args (ints/tuples) instead of accepting them.
    """
    if not color_str:
        return None
    if not isinstance(color_str, str):
        return None
    return parse_color_to_uno_int(color_str)


def _sheet_prefix_for_clip(range_name: str, resolved: Any) -> str:
    """Prefix including the separator, or '' when the clip stays unqualified.

    A prefix already on range_name wins. split_sheet_prefix turns 'O''Brien'
    into O'Brien; quote and double again on the way out. Keep '!' when the
    input used a bang, otherwise '.'. An unqualified name (a named range)
    takes the sheet from resolved.getSpreadsheet().getName() and always
    uses '.'.
    """
    # Bugfix: Clipped addresses for oversized reads lost sheet prefixes for named
    # ranges or lost quote-escaping on sheet names with apostrophes/special chars.
    # This happened because _format_sheet_address only looked at the input range_name
    # string and dropped quotes without re-escaping. _sheet_prefix_for_clip preserves
    # the original quotes and separator (or falls back to the resolved sheet) with
    # doubled apostrophes so Calc resolves clipped ranges on the proper sheet.
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


def _range_args(raw: Any) -> list[Any]:
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    return list(raw)


def _manipulator_context(ctx: ToolContext, range_arg: Any) -> tuple[CellManipulator, list[Any]]:
    return CellManipulator(CalcBridge(ctx.doc)), _range_args(range_arg)


def _compound_undo(doc: Any, title: str) -> Any:
    from plugin.doc.compound_undo import WriterCompoundUndo

    return WriterCompoundUndo(doc, title)


def _undo_action_count(doc: Any) -> int:
    def _do() -> int:
        if not hasattr(doc, "getUndoManager"):
            return 0
        um = doc.getUndoManager()
        if um is None:
            return 0
        titles = um.getAllUndoActionTitles()
        return len(titles) if titles is not None else 0

    return uno_probe(_do, 0)


def _undo_titled_group(doc: Any, title: str, prev_count: int = 0) -> bool:
    """Undo the group we just closed, only when its title is the newest one and count grew."""
    def _do_undo() -> bool:
        if not hasattr(doc, "getUndoManager"):
            return False
        um = doc.getUndoManager()
        if um is None:
            return False
        titles = um.getAllUndoActionTitles()
        if titles and len(titles) > prev_count and titles[0] == title:
            um.undo()
            return True
        return False

    return uno_probe(_do_undo, False)


def _mutate_ranges(
    tool: ToolBase,
    doc: Any,
    ranges: list[Any],
    undo_title: str,
    apply_one: Callable[[Any], str | dict[str, Any]],
    done_message: str,
    format_result: Callable[[list[Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run apply_one on each range inside one compound undo."""
    if len(ranges) == 0:
        return tool._tool_error("range is required")

    group = None
    target = ranges[0]
    applied = 0
    results: list[Any] = []
    prev_count = _undo_action_count(doc)
    try:
        group = _compound_undo(doc, undo_title)
        with group:
            for r in ranges:
                target = r
                res = apply_one(r)
                applied += 1
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
        if is_real_disposal(exc):
            raise
        undone = False
        if group is not None and getattr(group, "entered", False):
            undone = _undo_titled_group(doc, undo_title, prev_count)
        if undone:
            return tool._tool_error(f"Failed on range {target!r}: {exc}. Nothing was kept.")
        if applied > 0:
            if not (group is not None and getattr(group, "entered", False)):
                return tool._tool_error(f"Failed on range {target!r}: {exc}. {applied} range(s) were already changed individually.")
            return tool._tool_error(f"Failed on range {target!r}: {exc}. {applied} range(s) were already changed; one Ctrl+Z undoes them.")
        return tool._tool_error(str(exc))


def _preview_if_large(bridge: Any, range_name: str) -> dict[str, Any] | None:
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
        preview_cols = min(cols, _DISTINCT_PEEK_MAX_COLUMNS)
        end_row = int(addr.StartRow) + preview_rows - 1
        end_col = int(addr.StartColumn) + preview_cols - 1
        local = format_range(int(addr.StartColumn), int(addr.StartRow), end_col, end_row)

        prefix = _sheet_prefix_for_clip(range_name, cell_range)
        preview_range = f"{prefix}{local}"

        return {
            "rows": rows,
            "columns": cols,
            "cells": cells,
            "preview_range": preview_range,
            "sheet_prefix": prefix,
            "start_column": int(addr.StartColumn),
            "start_row": int(addr.StartRow),
        }
    except Exception as e:
        if is_real_disposal(e):
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
    # Bugfix: Numeric or empty top-row values were skipped from distinct counts
    # when a column had no text header. This happened because the function sliced
    # data_array[1:] globally across all columns. Checking each column's row 0 cell
    # allows text headers to label columns while non-text cells are counted as data
    # starting from row 0.
    if not isinstance(data_array, (list, tuple)) or not data_array:
        return []
    header_row = data_array[0]
    if not isinstance(header_row, (list, tuple)):
        return []
    n_cols = min(len(header_row), _DISTINCT_PEEK_MAX_COLUMNS)
    if n_cols <= 0:
        return []
    out: list[dict[str, Any]] = []
    for col_i in range(n_cols):
        raw_header = header_row[col_i] if col_i < len(header_row) else None
        if isinstance(raw_header, str) and raw_header.strip():
            label = raw_header.strip()
            scan_rows = data_array[1:]
        else:
            label = index_to_column(start_column + col_i)
            scan_rows = data_array
        # Track every distinct for the count; only the first LIST_CAP go in the payload.
        seen: set[str] = set()
        listed: list[str] = []
        overflow = False
        for row in scan_rows:
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


def _try_column_distinct_peek(bridge: Any, range_name: str, preview: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Scan a clipped window of the oversized range via getDataArray. Fail-soft."""
    try:
        start_col = int(preview["start_column"])
        start_row = int(preview["start_row"])
        cols = int(preview["columns"])
        rows = int(preview["rows"])
        end_col = start_col + min(cols, _DISTINCT_PEEK_MAX_COLUMNS) - 1
        end_row = start_row + min(rows, _DISTINCT_PEEK_MAX_ROWS) - 1
        local = format_range(start_col, start_row, end_col, end_row)
        clipped = f"{preview.get('sheet_prefix') or ''}{local}"
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
        if is_disposed_exception(e):
            raise
        log.exception("Could not compute column distinct peek for %s", range_name)
        return None


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


class ReadCellRange(ToolBase):
    """Read values from one or more cell ranges."""

    name: str | None = "read_cell_range"
    description: str = (
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
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"range": {"type": "array", "items": {"type": "string"}, "description": ('Cell range(s) (e.g. ["A1:D10"], ["Sheet1.A1:C5"], ["\'Data Sheet\'!B2"]). Sheet prefixes target that sheet without switching the active sheet.')}},
        "required": ["range"],
    }
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier: str = "core"
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        bridge = CalcBridge(ctx.doc)
        inspector = CellInspector(bridge)
        rn = _range_args(kwargs["range"])

        if len(rn) == 0:
            return self._tool_error("range is required")
        if len(rn) == 1:
            return self._read_one(bridge, inspector, rn[0])
        results = [self._read_one(bridge, inspector, r) for r in rn]
        failed = [item["range"] for item in results if item.get("status") != "ok"]
        if failed:
            return {
                "status": "error",
                "message": f"Failed to read {len(failed)} of {len(rn)} ranges: {failed!r}",
                "result": results,
            }
        # Preserve the old list-of-grids shape when nothing was truncated.
        if all(item.get("status") == "ok" and not item.get("truncated") for item in results):
            return {"status": "ok", "result": [item["result"][0] for item in results]}
        return {"status": "ok", "result": results}

    def _read_one(self, bridge: Any, inspector: Any, range_name: str) -> dict[str, Any]:
        """Read one range; preview-only when the full grid would swamp chat context."""
        try:
            preview = _preview_if_large(bridge, range_name)
            if preview is None:
                grid = inspector.read_range(range_name, include_format_info=True)
                return {"status": "ok", "result": [grid], "range": range_name}
            grid = inspector.read_range(preview["preview_range"], include_format_info=True)
            payload: dict[str, Any] = {
                "status": "ok",
                "truncated": True,
                "message": _READ_CELL_RANGE_TRUNCATED_MSG.format(rows=preview["rows"], columns=preview["columns"], cells=preview["cells"]),
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
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.exception("Failed to read range %s", range_name)
            return {"status": "error", "range": range_name, "message": str(exc) or type(exc).__name__}


class WriteCellRange(ToolBase):
    """Write formulas or values to a cell range."""

    name: str | None = "write_formula_range"
    description: str = (
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
                    '(e.g. \'["a", "b"]\' for 2 cells). Repeating the same '
                    "=B2*…-style formula in every element pins every row to the "
                    "first ref — use one formula string over the whole column "
                    "range instead. Empty string/array clears the range. "
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
        source, source_err = _normalize_source_arg(kwargs.get("source"))
        if source_err:
            return self._tool_error(source_err)

        if len(rn) == 0:
            return self._tool_error("range is required")

        # source XOR values: a paste-copy must not also write/clear a values payload.
        if source:
            if _values_conflict_with_source(kwargs):
                return self._tool_error("Do not pass values when source is set. To copy a block, pass source and dest range only.")

            def _apply_copy(dest: Any) -> dict[str, Any]:
                return manipulator.copy_formula_range(source, dest)

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
                _apply_copy,
                "",
                format_result=_format_copy,
            )

        fov = kwargs.get("values")
        # values is required when source is omitted (schema keeps it optional so
        # source-only calls pass validate). Missing must not fall through as clear.
        if fov is None:
            return self._tool_error("values is required when source is omitted")
        # Bugfix: top-level bool in values was converted to string "True"/"False"
        # instead of being rejected to match the tool parameter schema.
        if isinstance(fov, bool):
            return self._tool_error("values must be a string, number, or JSON array, not a boolean")
        # Normalize: schema is string for Gemini; accept number/list from other providers
        if isinstance(fov, (int, float)):
            fov = str(fov)

        single_cell = len(rn) == 1 and _a1_range_shape(rn[0]) == (1, 1, 1)
        parsed_vals = _parse_formula_or_values_string(fov, single_cell_range=single_cell) if isinstance(fov, str) else (fov if isinstance(fov, list) else None)
        if parsed_vals is not None and len(parsed_vals) > 0:
            if isinstance(parsed_vals[0], (list, tuple)):
                r_cnt = len(parsed_vals)
                c_cnt = len(parsed_vals[0]) if r_cnt > 0 else 0
                for r in rn:
                    shape = _a1_range_shape(r)
                    if shape is None:
                        continue
                    n_cells, rows, cols = shape
                    if n_cells > 1:
                        if not all(isinstance(row, (list, tuple)) and len(row) == c_cnt for row in parsed_vals) or r_cnt != rows or c_cnt != cols:
                            return self._tool_error(f"Array is {r_cnt}x{c_cnt} but range is {rows}x{cols}.")
            else:
                n_vals = len(parsed_vals)
                for r in rn:
                    shape = _a1_range_shape(r)
                    if shape is None:
                        continue
                    n_cells, rows, cols = shape
                    if n_vals != n_cells:
                        return self._tool_error(_values_length_mismatch_message(r, n_vals, n_cells, rows, cols))

        array_flag, err = _opt_bool(kwargs, "array")
        if err:
            return self._tool_error(err)

        first_sz = manipulator.prepare_array_formula_if_needed(rn[0], fov, array=array_flag)
        premeasured: list[tuple[int, int] | None] = [first_sz]
        for r in rn[1:]:
            sz = manipulator.prepare_array_formula_if_needed(r, fov, array=array_flag, premeasured_array_size=first_sz)
            premeasured.append(sz)

        idx = 0

        def _apply_write(r: Any) -> Any:
            nonlocal idx
            sz = premeasured[idx]
            idx += 1
            return manipulator.write_formula_range(r, fov, array=array_flag, premeasured_array_size=sz)

        def _format_write(results: list[Any]) -> dict[str, Any]:
            if len(rn) == 1:
                res = results[0]
                if isinstance(res, dict):
                    return {"status": "ok", **res}
                return {"status": "ok", "message": res}

            if any(isinstance(res, dict) for res in results):
                ranges_payload: list[dict[str, Any]] = []
                for r, res in zip(rn, results):
                    if isinstance(res, dict):
                        ranges_payload.append({"range": r, **res})
                    else:
                        ranges_payload.append({"range": r, "message": res})
                return {
                    "status": "ok",
                    "message": f"Wrote to {len(rn)} ranges",
                    "ranges": ranges_payload,
                }
            return {"status": "ok", "message": f"Wrote to {len(rn)} ranges"}

        return _mutate_ranges(
            self,
            ctx.doc,
            rn,
            "WriterAgent: Write formulas",
            _apply_write,
            f"Wrote to {len(rn)} ranges",
            format_result=_format_write,
        )


class InsertCellHtml(ToolBase):
    """Insert HTML as rich text into a single cell (active sheet)."""

    name: str | None = "insert_cell_html"
    intent: str | None = "edit"
    description: str = "Parses HTML with the same filter as Writer and pastes rich text into one cell on the active sheet (e.g. <b>, <i>, <a href>, line breaks). Does not support images or embedded objects. Clears existing cell text. Use set_style for table-wide borders."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"cell": {"type": "string", "description": 'Single cell (e.g. "A1") on the active sheet.'}, "html": {"type": "string", "description": "HTML fragment or small document (UTF-8)."}}, "required": ["cell", "html"]}
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.calc.address_utils import parse_address
        from plugin.calc.rich_html import insert_cell_html_rich

        addr = kwargs["cell"].strip()
        html = kwargs["html"]
        assert isinstance(html, str)
        if not addr:
            return self._tool_error("cell is required")
        try:
            parse_address(addr)
        except ValueError as e:
            return self._tool_error(f"Invalid cell address: {e}")

        config_svc = None
        if ctx.services is not None and hasattr(ctx.services, "get"):
            config_svc = ctx.services.get("config")

        insert_cell_html_rich(ctx.doc, ctx.ctx, addr, html, config_svc=config_svc)
        return {"status": "ok", "message": f"Inserted rich HTML into cell {addr.upper()}."}


class SetCellStyle(ToolBase):
    """Apply style and formatting to cells or ranges."""

    name: str | None = "set_style"
    intent: str | None = "edit"
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

        bg_color, err = _opt_color(kwargs, "bg_color")
        if err:
            return self._tool_error(err)

        font_color, err = _opt_color(kwargs, "font_color")
        if err:
            return self._tool_error(err)

        border_color, err = _opt_color(kwargs, "border_color")
        if err:
            return self._tool_error(err)

        # Direct execute() skips validate; these type checks stay so
        # tests/calc/test_cells.py still gets a tool error instead of a silent no-op.
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

        bold, err = _opt_bool(kwargs, "bold")
        if err:
            return self._tool_error(err)
        italic, err = _opt_bool(kwargs, "italic")
        if err:
            return self._tool_error(err)
        wrap_text, err = _opt_bool(kwargs, "wrap_text")
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

        style_kwargs: dict[str, Any] = {"bold": bold, "italic": italic, "bg_color": bg_color, "font_color": font_color, "font_size": font_size, "h_align": h_align, "v_align": v_align, "wrap_text": wrap_text, "border_color": border_color, "number_format": number_format}

        if len(rn) == 0:
            return self._tool_error("range is required")

        # Bugfix: Style calls with no properties performed empty mutations and opened
        # useless undo steps. Checking if all style properties are None avoids this.
        if all(v is None for v in style_kwargs.values()):
            return self._tool_error("No style properties were provided.")

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
    intent: str | None = "edit"
    description: str = "Merges the specified cell range(s). Typically used for main headers. Write text with write_formula_range and style with set_style after merging. Supports lists for non-contiguous areas."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"range": {"type": "array", "items": {"type": "string"}, "description": ('Range(s) to merge (e.g. ["A1:D1"] or ["A1:B1", "C1:D1"]).')}, "center": {"type": "boolean", "description": "Center content (default: true)"}},
        "required": ["range"],
    }
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        manipulator, rn = _manipulator_context(ctx, kwargs["range"])
        center_val, err = _opt_bool(kwargs, "center")
        if err:
            return self._tool_error(err)
        center = True if center_val is None else center_val

        def _apply_merge(r: Any) -> str:
            manipulator.merge_cells(r, center=center)
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

    name: str | None = "sort_range"
    intent: str | None = "edit"
    # Action-time key/direction and "use this tool, don't rewrite the
    # block" live here. Do not restack another SORT Don't/Do into
    # CALC_CORE (flash merges adjacent identical shapes).
    description: str = (
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

        sort_column_val, err = _strict_int(kwargs, "sort_column", 0)
        if err:
            return self._tool_error(err)
        sort_column = 0 if sort_column_val is None else sort_column_val

        ascending_val, err = _opt_bool(kwargs, "ascending")
        if err:
            return self._tool_error(err)
        ascending = True if ascending_val is None else ascending_val

        has_header: bool = kwargs["has_header"]
        assert isinstance(has_header, bool)

        def _apply_sort(r: Any) -> str:
            return manipulator.sort_range(r, sort_column=sort_column, ascending=ascending, has_header=has_header)

        return _mutate_ranges(
            self,
            ctx.doc,
            rn,
            "WriterAgent: Sort range",
            _apply_sort,
            f"Sorted {len(rn)} ranges",
        )


class DeleteStructure(ToolBase):
    """Delete rows or columns."""

    name: str | None = "delete_structure"
    intent: str | None = "edit"
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
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        # Bugfix: delete_structure accepted invalid start values (e.g. non-ASCII digits like '²',
        # floats, or column '3' which produced negative indices) and invalid counts (bools, floats).
        # This happened because start and count were passed directly without validating
        # types and ranges. Validating start and count strictly returns tool errors before
        # modifying document structures.
        structure_type: str = kwargs["structure_type"]
        assert structure_type in ("rows", "columns")

        count_val, err = _strict_int(kwargs, "count", 1)
        if err:
            return self._tool_error(err)
        count = 1 if count_val is None else count_val

        start_raw = kwargs["start"]
        if structure_type == "rows":
            if type(start_raw) is int:
                row_val = start_raw
            elif isinstance(start_raw, str) and start_raw.isascii() and start_raw.isdigit():
                row_val = int(start_raw)
            else:
                return self._tool_error('start for rows must be a 1-based row number (for example 5 or "5")')
            if row_val < 1:
                return self._tool_error('start for rows must be a 1-based row number (for example 5 or "5")')
            start: int | str = row_val
        else:
            if not (isinstance(start_raw, str) and start_raw.isascii() and start_raw.isalpha()):
                return self._tool_error('start for columns must be a column letter (for example "C")')
            start = start_raw.upper()

        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        try:
            result = manipulator.delete_structure(structure_type, start, count=count)
            return {"status": "ok", "message": result}
        except Exception as e:
            # Bugfix: a closed document raises DisposedException from delete_structure, and this
            # except turned that into a tool error.
            # Re-raise real disposal so execute_safe reports DOCUMENT_DISPOSED.
            # Any other failure stays on the existing error path.
            if is_real_disposal(e):
                raise
            return self._tool_error(str(e))
