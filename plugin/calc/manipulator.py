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
"""Cell manipulator — writing data and formatting LibreOffice Calc cells.

UNO imports are deferred to method bodies.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from itertools import chain
import logging
import re
from typing import TYPE_CHECKING, Any, Callable, Generator, Literal, NamedTuple, Sequence, TypeGuard

if TYPE_CHECKING:
    from com.sun.star.awt import FontWeight
    from com.sun.star.awt.FontSlant import ITALIC, NONE
    from com.sun.star.table.CellHoriJustify import CENTER, LEFT, RIGHT, BLOCK, STANDARD
    from com.sun.star.table import BorderLineStyle, TableSortField
    from plugin.calc.python.diagnostics import DiagnosticEntry
    from plugin.calc.python.formula_edit import PyFormula
else:
    try:
        from com.sun.star.awt import FontWeight
        from com.sun.star.awt.FontSlant import ITALIC, NONE
        from com.sun.star.table.CellHoriJustify import CENTER, LEFT, RIGHT, BLOCK, STANDARD
        from com.sun.star.table import BorderLineStyle, TableSortField
    except ImportError:
        pass


from plugin.calc.address_utils import (
    MAX_COL,
    MAX_ROW,
    column_to_index,
    format_address,
    format_range,
    index_to_column,
    local_a1,
    parse_range_string,
    qualify_a1,
)
from plugin.calc.array_formula import MAX_ARRAY_CELLS, plan_target, returns_array
from plugin.calc.bridge import is_agent_visible_sheet
from plugin.calc.calc_utils import NumberFormatCategories, elapsed_format_key, resolve_document_locale
from plugin.calc.error_detector import formula_error_cells
from plugin.calc.formula_dialect import FormulaDialects, make_cell_address
from plugin.calc.formula_fill import expand_single_formula, repeated_formula_columns
from plugin.calc.python.addin_values import accepts_returned_any
from plugin.calc.python.cell_discovery import py_formula_or_none
from plugin.calc.python.diagnostics import diagnostics_workbook_key, get_diagnostics_store
from plugin.calc.python.formula_edit import repair_py_code_newlines
from plugin.calc.python.geometric_recalc_core import arg_covers_cell
from plugin.calc.datetime_wire import (
    coalesce_temporal_apply_rects,
    duration_serial_from_iso,
    is_compatible_temporal_template,
    match_iso_duration,
    match_iso_temporal,
    should_preserve_temporal_format,
)
from plugin.doc.compound_undo import UndoStepOrRollback
from plugin.framework.errors import CalcError, Diagnostic, WriterAgentException, best_effort, is_disposal, suggest_call, uno_read
from plugin.framework.i18n import _
from plugin.framework.json_utils import safe_json_loads
from plugin.framework.thread_guard import unwrap_uno
from plugin.framework.uno_context import get_ctx


log = logging.getLogger("writeragent.calc")

MAX_WRITE_CELLS = 250_000
_ARRAY_PREVIEW_ROWS = 5
_ARRAY_PREVIEW_COLS = 10
_MAX_REPORTED_CELLS = 10
_CELL_FLAG_FORMULA = 16  # com.sun.star.sheet.CellFlags.FORMULA
# Per-range write-report keys merged by multi-range callers.
WRITE_REPORT_KEYS: tuple[str, ...] = ("error_cells", "text_that_looks_numeric", "apostrophe_removed", "warnings")
_PY_PLACEMENT_SCAN_COLS = 26
_BORDER_LINE_WIDTH = 50  # 1/100 mm
# com.sun.star.util.NumberFormat type constants (offapi/com/sun/star/util/NumberFormat.idl).
_NUMBER_FORMAT_PRESETS: dict[str, int] = {"general": 16, "currency": 8, "percent": 128}

# Calc declares VertJustify as a long (sc/source/ui/unoobj/cellsuno.cxx,
# SC_UNONAME_CELLVJUS). SvxVerJustifyItem::PutValue
# (editeng/source/items/justifyitem.cxx) reads only a CellVertJustify2 integer;
# any CellVertJustify enum value fails that read and leaves the cell at STANDARD.
_VERT_JUSTIFY: dict[str, int] = {"standard": 0, "top": 1, "center": 2, "bottom": 3}


class _CommitResult(NamedTuple):
    data_array: list[list[Any]]
    dest_categories: dict[tuple[int, int], str | None]
    categories: NumberFormatCategories
    cols_needing: dict[int, str]
    py_warning: str


class MergeOutcome(NamedTuple):
    merged: bool
    hidden_count: int
    hidden_cells: list[str]


class _DeleteHeldBack(Exception):
    """Internal sentinel that rolls back a delete that would break formulas.

    Using a private sentinel ensures that only this refusal becomes
    ``REFS_WOULD_BREAK``, while UNO and disposal errors from ``removeByIndex``
    keep propagating unchanged.
    """


def _ref_formula_cells(doc: Any) -> set[tuple[str, int, int]]:
    """Return ``(sheet_name, col, row)`` for every formula cell containing ``#REF!`` on visible sheets.

    ``getFormulaArray`` formats formulas with the English API grammar
    (``sc/source/ui/unoobj/cellsuno.cxx``, ``lcl_GetInputString``), so ``#REF!``
    is locale-independent.
    """
    refs: set[tuple[str, int, int]] = set()
    sheets = doc.getSheets()
    for sheet_idx in range(sheets.getCount()):
        sheet = sheets.getByIndex(sheet_idx)
        name = str(sheet.getName())
        if not is_agent_visible_sheet(name):
            continue
        formula_ranges = sheet.queryContentCells(_CELL_FLAG_FORMULA)
        for ra in formula_ranges.getRangeAddresses():
            sc, sr, ec, er = int(ra.StartColumn), int(ra.StartRow), int(ra.EndColumn), int(ra.EndRow)
            formulas = sheet.getCellRangeByPosition(sc, sr, ec, er).getFormulaArray()
            for r_off, row in enumerate(formulas):
                for c_off, text in enumerate(row):
                    if isinstance(text, str) and "#REF!" in text:
                        refs.add((name, sc + c_off, sr + r_off))
    return refs


def _ref_named_ranges(doc: Any) -> set[tuple[str, str]]:
    """Return ``(scope, name)`` for every named range whose definition contains ``#REF!``.

    *scope* is ``"global"`` for workbook names, or the sheet name for sheet-local names on
    agent-visible sheets. These are the same scope values ``named_range_list`` reports and
    ``named_range_edit`` accepts. A row or column delete that removes every cell a name
    points at leaves ``$Sheet1.$A$#REF!`` in its definition. Formulas that use the name
    (``=MYNAME``) then show #REF! even though their own text has no ``#REF!``, so the
    formula-cell scan alone would let such a delete through silently.
    ``XNamedRange.getContent`` formats with the English API grammar
    (sc/source/ui/unoobj/nameuno.cxx, ScNamedRangeObj::getContent, GRAM_API), so the
    check does not depend on the UI language.
    """
    containers: list[tuple[str, Any]] = [("global", doc.NamedRanges)]
    sheets = doc.getSheets()
    for sheet_idx in range(sheets.getCount()):
        sheet = sheets.getByIndex(sheet_idx)
        sheet_name = str(sheet.getName())
        if is_agent_visible_sheet(sheet_name):
            containers.append((sheet_name, sheet.NamedRanges))
    return {
        (scope, str(n))
        for scope, nrs in containers
        for n in nrs.getElementNames()
        if "#REF!" in str(nrs.getByName(n).getContent())
    }


def _broken_refs_summary(cell_count: int, cell_sample: list[str], names: list[tuple[str, str]]) -> str:
    """Describe broken formula cells and named ranges for the refusal and warning messages."""
    parts: list[str] = []
    if cell_count:
        parts.append(f"{cell_count} formula cell(s) ({', '.join(cell_sample)})")
    if names:
        labels = ", ".join(
            nm if sc == "global" else f"{nm} (sheet {sc})"
            for sc, nm in names[:_MAX_REPORTED_CELLS]
        )
        parts.append(f"{len(names)} named range(s) ({labels})")
    return " and ".join(parts)


def _surviving_ref_cells(
    cells: set[tuple[str, int, int]],
    sheet_name: str,
    rows: bool,
    index: int,
    count: int,
) -> set[tuple[str, int, int]]:
    """Map pre-delete ``#REF!`` coordinates to their post-delete positions.

    A ``#REF!`` that existed before the delete moves with its row or column
    and must not be counted as newly broken.
    """
    surviving: set[tuple[str, int, int]] = set()
    end = index + count
    for s_name, col, row in cells:
        if s_name != sheet_name:
            surviving.add((s_name, col, row))
            continue
        pos = row if rows else col
        if index <= pos < end:
            continue
        shifted = pos - count if pos >= end else pos
        surviving.add((s_name, col, shifted) if rows else (s_name, shifted, row))
    return surviving


def _structure_index(structure_type: str, start: Any) -> int:
    """Return the 0-based row or column index for *start*."""
    if structure_type == "rows":
        return int(start) - 1
    return column_to_index(str(start).upper())


class _CellWrite(NamedTuple):
    data: Any
    formula: str
    kind: str
    input_category: str | None
    detected_key: int | None


class WritePlan(NamedTuple):
    """One range's write, decided by ``CellManipulator.plan_write`` before any cell changes.

    ``kind`` is ``"clear"`` (empty values cleared at commit), ``"array"`` (a dynamic-array
    formula measured with ``_probe_array_formula``), or ``"cells"`` (a row-major ``values``
    list ready for ``plan_writes``/``commit``). ``expanded`` is ``True`` when a 2-D array
    targeted a single cell and ``range_str`` / ``end`` grew to match the array's size.
    ``notes`` holds planner ``Diagnostic`` warnings attached to the commit result.
    """

    kind: Literal["clear", "array", "cells"]
    range_str: str
    sheet: Any = None
    start: tuple[int, int] = (0, 0)
    end: tuple[int, int] = (0, 0)
    values: Any = None
    array_size: tuple[int, int] | None = None
    expanded: bool = False
    literal_text: bool = False
    data_text: bool = False
    notes: tuple[Diagnostic, ...] = ()


def _calc_grammar_in_values(dialects: FormulaDialects, items: list[Any], pos: Any = None) -> list[Any]:
    """Every formula in *items* in Calc's API grammar.

    Excel-style text (commas, Sheet!A1, {1,2;3,4}) is translated by
    ``FormulaDialects``; Calc text is returned as sent.
    """
    return [dialects.to_api(v, pos) if isinstance(v, str) and v.startswith("=") else v for v in items]


def _with_notes(result: dict[str, Any], notes: Sequence[Diagnostic]) -> dict[str, Any]:
    if notes:
        result["warnings"] = [*notes, *result.get("warnings", [])]
    return result


def _values_length_mismatch_message(range_name: str, n_vals: int, n_cells: int, rows: int, cols: int) -> str:
    """Loud error so the model can resize the array or the range (no silent zip/pad)."""
    hint = ""
    if cols == 1 and rows > 1:
        hint = " Write one value per row (each formula uses that row's cells)."
    elif rows == 1 and cols > 1:
        hint = " Write one value per column."
    return f"Array has {n_vals} values but range {range_name} has {n_cells} cells ({rows}×{cols}). JSON array must match range size exactly, or pass a single string to fill the whole range.{hint}"


def _first_ragged_row(values: Sequence[Any]) -> int | None:
    """Return the 0-based index of the first row that is not a sequence matching row 1's length."""
    width = len(values[0])
    for idx, row in enumerate(values):
        if not isinstance(row, (list, tuple)) or len(row) != width:
            return idx
    return None


def _grid_mismatch_message(
    range_name: str,
    values: Sequence[Any],
    rows: int,
    cols: int,
    start: tuple[int, int],
    bad_row: int | None,
) -> str:
    """Explain a ragged or wrong-shaped 2-D write with the range name and the matching shape."""
    r = len(values)
    width = len(values[0])
    if bad_row is not None:
        row = values[bad_row]
        if not isinstance(row, (list, tuple)):
            return f"values row {bad_row + 1} is not a list (row 1 has {width} items); every row needs the same length. Range {range_name} is {rows}×{cols}."
        return f"values row {bad_row + 1} has {len(row)} items but row 1 has {width}; every row needs the same length. Range {range_name} is {rows}×{cols}."
    return f"values is {r}×{width} (rows×columns) but range {range_name} is {rows}×{cols}. Send {rows} rows of {cols} values, or use a {r}×{width} range starting at {format_address(*start)}."


SortKey = tuple[int, bool]  # (0-based column inside the range, ascending)


class SortPlan(NamedTuple):
    """One range's sort, checked by ``CellManipulator.plan_sort`` before any row moves."""

    range_str: str
    cell_range: Any
    height: int
    keys: Sequence[SortKey]


def parse_then_by(then_by: Any) -> list[SortKey] | None:
    """Validate and normalize ``then_by`` tie-break items; shared with the string harness."""
    if not isinstance(then_by, list):
        return None
    parsed: list[SortKey] = []
    for item in then_by:
        if not isinstance(item, dict) or set(item.keys()) != {"column", "ascending"}:
            return None
        col = item["column"]
        if isinstance(col, float) and col.is_integer():
            col = int(col)
        if type(col) is not int or col < 0:
            return None
        asc = item["ascending"]
        if not isinstance(asc, bool):
            return None
        parsed.append((col, asc))
    return parsed


def _sort_keys_width_error(range_str: str, keys: Sequence[SortKey], width: int) -> str | None:
    """Return the first out-of-range key error for a range of *width* columns; shared with the string harness."""
    for idx, (col, _unused) in enumerate(keys):
        if not 0 <= col < width:
            label = "sort_column" if idx == 0 else "then_by column"
            return f"{label} must be an integer from 0 to {width - 1} for range {range_str} ({width} columns)."
    return None


def _sort_done_message(range_str: str, keys: Sequence[SortKey]) -> str:
    """Return the sort completion message for one or more keys; shared with the string harness."""
    if len(keys) == 1:
        col, asc = keys[0]
        direction = "ascending" if asc else "descending"
        return f"Range {range_str} sorted {direction} by column {col}."
    parts = ", then ".join(f"column {col} {'ascending' if asc else 'descending'}" for col, asc in keys)
    return f"Range {range_str} sorted by {parts}."


def _sort_unchanged_message(range_str: str, keys: Sequence[SortKey]) -> str:
    """Return the no-op sort message telling the caller what to check instead of repeating; shared with the string harness."""
    if len(keys) == 1:
        return (
            f"Range {range_str}: key column {keys[0][0]} unchanged by the sort; no rows moved. "
            "Calling it again changes nothing; if rows should move, check range, has_header and sort_column."
        )
    col_list = ", ".join(str(col) for col in dict.fromkeys(col for col, _unused in keys))
    return (
        f"Range {range_str}: key columns {col_list} unchanged by the sort; no rows moved. "
        "Calling it again changes nothing; if rows should move, check range, has_header, sort_column and then_by."
    )


def _write_summary(
    range_str: str,
    cell_metas: Sequence[_CellWrite],
    suffix: str,
    overwrite_warning: str | None,
) -> str:
    """The one-line summary the agent gets after a cell write: how many of each kind landed, plus any format or overwrite warning."""
    counts = Counter(
        (m.input_category or "temporal") if m.kind == "temporal"
        else "text" if m.kind in ("text", "forced_text")
        else m.kind
        for m in cell_metas
        if m.kind in ("temporal", "text", "forced_text", "number", "formula")
    )

    parts = []
    for name in ("date", "time", "datetime", "duration", "number", "text", "formula"):
        n = counts[name]
        if n:
            parts.append(f"{n} {name}" + ("s" if n != 1 and name != "text" else ""))
    detail = f" ({', '.join(parts)})" if parts else ""
    n_vals = len(cell_metas)
    values_word = "value" if n_vals == 1 else "values"
    msg = f"Range {range_str} filled with {n_vals} {values_word}{detail}{suffix}."

    if overwrite_warning:
        msg = f"{msg} ({overwrite_warning})"
    return msg


_WRITE_VALUES_CELL_ERR = (
    "Each cell in values must be string, number, boolean, or null; got {kind}."
)

_NUMBER_LIKE_RE = re.compile(
    r"[+-]?[$€£¥]?\s?(?:\d{1,3}(?:[,.' \u00a0\u202f]\d{3})+(?:[.,]\d+)?|\d+[.,]\d+|\d+)\s?[%€$£¥]?"
)
_DATE_LIKE_RE = re.compile(
    r"(?:\d{1,2}|\d{4})([./-])\d{1,2}\1(?:\d{2}|\d{4})(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AaPp][Mm])?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|\d{1,2}:\d{2}(?::\d{2})?\s?[AaPp][Mm]"
)


def looks_numeric_text(text: str) -> bool:
    """Return True if *text* matches a number-like or date/time-like shape.

    These are shapes only, the same for every locale, and only emit warnings,
    never convert values. Only plain numbers and ISO 8601 become values
    (see ``_classify_write_cell``).
    """
    s = text.strip()
    return bool(_NUMBER_LIKE_RE.fullmatch(s) or _DATE_LIKE_RE.fullmatch(s))


def _apostrophe_only_dropped(rest: str) -> bool:
    """Return True when a leading apostrophe protected nothing and was only stripped.

    Calc strips one leading apostrophe on input (``ScColumn::ParseString`` in
    ``sc/source/core/data/column3.cxx``), so the write does too. This reports
    the cells where the apostrophe was not protecting a formula, doubled
    apostrophe, ISO temporal/duration, numeric-looking text, or float.
    """
    if rest.startswith(("=", "'")):
        return False
    s = rest.strip()
    if match_iso_temporal(s) or match_iso_duration(s) or looks_numeric_text(s):
        return False
    try:
        float(rest)
        return False
    except ValueError:
        return True


def _text_write_notes(values: Sequence[Any], cell_metas: Sequence[_CellWrite]) -> tuple[list[int], list[int]]:
    """Return flat indexes of text cells that look numeric and cells whose leading apostrophe was only dropped."""
    numeric_idx: list[int] = []
    apostrophe_idx: list[int] = []
    for idx, (value, meta) in enumerate(zip(values, cell_metas)):
        if not isinstance(value, str):
            continue
        if meta.kind == "text" and looks_numeric_text(value):
            numeric_idx.append(idx)
        elif meta.kind == "forced_text" and value.startswith("'") and _apostrophe_only_dropped(value[1:]):
            apostrophe_idx.append(idx)
    return numeric_idx, apostrophe_idx


def _write_report(
    sheet: Any,
    start: tuple[int, int],
    end: tuple[int, int],
    values: Sequence[Any],
    cell_metas: Sequence[_CellWrite],
    *,
    scan_text: bool,
) -> dict[str, Any]:
    """Build optional error/text-note report keys and Diagnostic warnings for a committed write.

    Errors are read after the whole range lands because reading ``cell.Error``
    before later formula cells are set gives false errors on forward references,
    and ``queryFormulaCells`` evaluates dirty cells so no recalc is needed.
    Egress modes pass ``scan_text=False`` because they store text verbatim and
    their callers ignore the result.
    """
    report: dict[str, Any] = {}
    warnings: list[Diagnostic] = []

    if any(m.kind == "formula" for m in cell_metas):
        errors, total = formula_error_cells(
            sheet,
            sheet.getCellRangeByPosition(start[0], start[1], end[0], end[1]),
            limit=_MAX_REPORTED_CELLS,
        )
        if errors:
            report["error_cells"] = [
                {"address": e["address"], "error": e["error"]["code"], "formula": e["formula"]}
                for e in errors
            ]
            warn_msg = (
                f"{total} formula cell(s) show an error after the write "
                f"(error_cells lists {'the first ' + str(len(errors)) if total > len(errors) else 'them'}). "
                "A text label that starts with '=' must be written with a leading apostrophe ('=label). "
                "Use detect_errors on a cell for the cause."
            )
            # Err:539 is unsupported inline array content ({1,2;3,4}), 525 is unresolved Sheet!A1, and
            # 501/508/509 cover bad separators/brackets. FormulaDialects.to_api already translated
            # single-dialect formulas before writing, so a failure still containing ',' or '!' means a mixed formula.
            if any(
                e["error"]["error_code"] in (501, 508, 509, 525, 539)
                and ("," in e["formula"] or "!" in e["formula"])
                for e in errors
            ):
                warn_msg += (
                    " Formulas use Calc syntax: ; between arguments, Sheet.A1 for other sheets,"
                    " and {1;2|3;4} for inline arrays (e.g. =IF(A1>0; 1; 0), Orders.A1)."
                )
            warnings.append({"code": "FORMULA_ERROR", "message": warn_msg})

    if scan_text:
        numeric_idx, apostrophe_idx = _text_write_notes(values, cell_metas)
        if numeric_idx or apostrophe_idx:
            sheet_name = str(sheet.getName())
            num_cols = end[0] - start[0] + 1
            if numeric_idx:
                report["text_that_looks_numeric"] = [
                    {
                        "address": qualify_a1(sheet_name, format_address(start[0] + i % num_cols, start[1] + i // num_cols)),
                        "value": values[i].strip(),
                    }
                    for i in numeric_idx[:_MAX_REPORTED_CELLS]
                ]
                warnings.append({
                    "code": "TEXT_LOOKS_NUMERIC",
                    "message": (
                        f"{len(numeric_idx)} cell(s) look like a number or date but were stored as text "
                        "(text_that_looks_numeric). Only plain numbers (1.5) and ISO dates (2026-08-05) "
                        "become values; resend them that way if they must calculate, or leave them if they are labels."
                    ),
                })
            if apostrophe_idx:
                report["apostrophe_removed"] = [
                    {
                        "address": qualify_a1(sheet_name, format_address(start[0] + i % num_cols, start[1] + i // num_cols)),
                        "value": cell_metas[i].data,
                    }
                    for i in apostrophe_idx[:_MAX_REPORTED_CELLS]
                ]
                warnings.append({
                    "code": "APOSTROPHE_REMOVED",
                    "message": (
                        f"{len(apostrophe_idx)} cell(s) started with an apostrophe, which was removed as the "
                        "force-text marker (apostrophe_removed). To keep a leading apostrophe in the text, "
                        "double it (''90s)."
                    ),
                })

    if warnings:
        report["warnings"] = warnings
    return report


def _write_values_cell_ok(cell: Any) -> bool:
    if cell is None:
        return True
    return isinstance(cell, (str, int, float, bool))


def _write_values_grid_cell_error(parsed_vals: list[Any]) -> str | None:
    """Reject nested structures before UNO."""
    if not parsed_vals:
        return None
    if isinstance(parsed_vals[0], (list, tuple)):
        for row in parsed_vals:
            if not isinstance(row, (list, tuple)):
                return _WRITE_VALUES_CELL_ERR.format(kind=type(row).__name__)
        cells: Any = chain.from_iterable(parsed_vals)
    else:
        cells = parsed_vals
    for cell in cells:
        if not _write_values_cell_ok(cell):
            return _WRITE_VALUES_CELL_ERR.format(kind=type(cell).__name__)
    return None


def _check_write_cap(n_cells: int) -> None:
    if n_cells > MAX_WRITE_CELLS:
        raise CalcError(f"The range has {n_cells} cells, over the {MAX_WRITE_CELLS}-cell write limit. Write a smaller range.")



def _requests_array_formula(formula_or_values: Any, array: bool | None) -> TypeGuard[str]:
    if array is True and isinstance(formula_or_values, str) and not formula_or_values.startswith("="):
        raise CalcError("array=true needs a formula starting with '='.")
    return isinstance(formula_or_values, str) and returns_array(formula_or_values, forced=array)


# ── Helper ─────────────────────────────────────────────────────────────


def _uno_range_address(cell_or_range: Any) -> Any:
    """Normalize a UNO cell or range to ``CellRangeAddress``.

    ``resolve_range_or_address`` returns an ``XCell`` for a single cell
    (``getCellAddress`` only) and an ``XCellRange`` for A1:B2 / named ranges
    (``getRangeAddress``). ``copyRange`` needs the latter shape.
    """
    if hasattr(cell_or_range, "getRangeAddress"):
        return cell_or_range.getRangeAddress()
    cell_addr = cell_or_range.getCellAddress()
    from com.sun.star.table import CellRangeAddress

    ra = CellRangeAddress()
    ra.Sheet = cell_addr.Sheet
    ra.StartColumn = cell_addr.Column
    ra.EndColumn = cell_addr.Column
    ra.StartRow = cell_addr.Row
    ra.EndRow = cell_addr.Row
    return ra


def quiet_probe_cleanup(action: Callable[[], None], what: str) -> None:
    """Best-effort probe teardown that still surfaces a disposed document."""
    # Re-raise disposal during array-formula probe cleanup; suppress other probe teardown errors.
    try:
        action()
    except Exception as e:
        if is_disposal(e):
            raise
        log.debug("%s failed during array probe cleanup", what, exc_info=True)


def _clear_array_range(rng: Any) -> None:
    rng.setArrayFormula("")
    rng.clearContents(23)


def _range_has_blank_cell(sheet: Any, start: tuple[int, int], end: tuple[int, int]) -> bool:
    """True when any cell in the inclusive box is blank.

    setDataArray undo does not clear a cell that was empty before the write,
    so a small range that overlaps a blank cell has to be written per cell.
    """
    for r in range(start[1], end[1] + 1):
        for c in range(start[0], end[0] + 1):
            cell = sheet.getCellByPosition(c, r)
            if cell.getFormula() == "" and cell.getString() == "" and cell.getValue() == 0.0:
                return True
    return False


def _number_format_keys(sheet: Any, start: tuple[int, int], end: tuple[int, int]) -> dict[tuple[int, int], int]:
    """Prior NumberFormat key of every cell in the inclusive box, one read per format group.

    getUniqueCellFormatRanges walks the box natively and returns one SheetCellRanges per
    distinct cell pattern, so a uniform column costs three UNO calls instead of two per cell.
    Cells in a group share one pattern, so the group's NumberFormat is each cell's key.
    """
    keys: dict[tuple[int, int], int] = {}
    groups = sheet.getCellRangeByPosition(start[0], start[1], end[0], end[1]).getUniqueCellFormatRanges()
    for idx in range(groups.getCount()):
        group = groups.getByIndex(idx)
        key = int(group.getPropertyValue("NumberFormat"))
        for addr in group.getRangeAddresses():
            for row in range(addr.StartRow, addr.EndRow + 1):
                for col in range(addr.StartColumn, addr.EndColumn + 1):
                    keys[(col, row)] = key
    return keys


def _occupied_cells(
    sheet: Any,
    left: int,
    top: int,
    right: int,
    bottom: int,
    *,
    skip: Sequence[tuple[int, int, int, int]] = (),
) -> list[tuple[int, int]]:
    """Return ``(col, row)`` coordinates of non-empty cells in the inclusive box."""
    formulas = sheet.getCellRangeByPosition(left, top, right, bottom).getFormulaArray()
    return [
        (left + c, top + r)
        for r, row in enumerate(formulas)
        for c, text in enumerate(row)
        if text and not any(b[0] <= left + c <= b[2] and b[1] <= top + r <= b[3] for b in skip)
    ]


def refuse_occupied_cells(sheet: Any, left: int, top: int, right: int, bottom: int, *, skip: Sequence[tuple[int, int, int, int]] = ()) -> None:
    """Raise CalcError when a cell in the inclusive box holds a value, text or formula.

    The box is read once with getFormulaArray. Calc builds each entry with the
    same input-string helper as XCell.getFormula (sc/source/ui/unoobj/cellsuno.cxx,
    lcl_GetInputString, shared by ScCellRangeObj::getFormulaArray and
    ScCellObj::GetInputString_Impl), so an entry is "" exactly when the cell is
    empty; a cell with only a comment or a format counts as empty. Cells inside
    a *skip* box (left, top, right, bottom) are not checked: the array formula
    being replaced and the formula's own origin. Nothing has been written when
    this raises, so the caller can refuse cleanly.
    """
    occupied = _occupied_cells(sheet, left, top, right, bottom, skip=skip)
    if occupied:
        needed = format_range(left, top, right, bottom, collapse_single=True)
        first_c, first_r = occupied[0]
        raise CalcError(
            "The result needs %s, but %d cell(s) there are not empty (first: %s). Nothing was written; clear them or start elsewhere."
            % (needed, len(occupied), format_address(first_c, first_r)),
            code="SPILL_BLOCKED",
            details={
                "needed_range": needed,
                "blocking_cells": [format_address(c, r) for c, r in occupied[:10]],
                "blocking_count": len(occupied),
            },
        )


def _free_cell_right_of(sheet: Any, sheet_name: str, arg: str, args: list[str]) -> str | None:
    """Find the first free column right of *arg* on its top row, or None."""
    local = local_a1(arg)
    if not local:
        return None
    try:
        (c0, r0), (c1, r1) = parse_range_string(local)
    except (TypeError, ValueError):
        return None
    top = min(r0, r1)
    bottom = max(r0, r1)
    right = max(c0, c1)
    scan_end = min(MAX_COL, right + _PY_PLACEMENT_SCAN_COLS)
    for col in range(right + 1, scan_end + 1):
        candidate = format_address(col, top)
        if any(arg_covers_cell(other, candidate, sheet_name=sheet_name) for other in args):
            continue
        if not _occupied_cells(sheet, col, top, col, bottom):
            return qualify_a1(sheet_name, candidate)
    return None


def _latest_py_diagnostic(doc: Any, parts: PyFormula | None) -> DiagnosticEntry | None:
    if parts is None or parts.code_is_ref:
        return None
    return best_effort(
        lambda: get_diagnostics_store().latest_for_code(diagnostics_workbook_key(doc), parts.code),
        "lookup py diagnostics",
    )


def _py_run_detail(doc: Any, parts: PyFormula, prior: DiagnosticEntry | None) -> str:
    """Return the =PY run's new error text from diagnostics, or empty string."""
    # The cell itself can only show Err:519 or #SPILL!. The add-in records a
    # new entry in the diagnostics store on every failed run; comparing
    # identity against the entry present before setFormula ignores older runs
    # without relying on wall-clock timestamps.
    latest = _latest_py_diagnostic(doc, parts)
    if latest is None or latest is prior or not latest.is_error:
        return ""
    return f" ({' '.join(latest.message.split())[:400]})"


def _parse_json_array_string(s: str) -> list[Any] | None:
    """Return the list when *s* is a JSON array string (semicolons outside quotes read as commas), else None.

    A model often sends the array as a JSON string, sometimes with Calc-style
    semicolons (e.g. ``'["Name"; "Category"; "Value"]'``).
    """
    s_strip = s.strip()
    if not s_strip:
        return None

    if s_strip.startswith("["):
        # Replace semicolons NOT inside double quotes with commas.
        normalized_list = []
        in_quotes = False
        escaped = False
        for char in s_strip:
            if char == '"' and not escaped:
                in_quotes = not in_quotes
            if char == ";" and not in_quotes:
                normalized_list.append(",")
            else:
                normalized_list.append(char)
            if char == "\\" and not escaped:
                escaped = True
            else:
                escaped = False

        normalized = "".join(normalized_list)
        data = safe_json_loads(normalized)
        if isinstance(data, list):
            return data

    return None


def _delimited_text_message(range_name: str, s: str, *, single_cell: bool) -> str | None:
    """Return the refusal text when a plain string would be ambiguous as one value, else None."""
    # A plain string is one cell value. A model that sends a whole table as one
    # delimited string would otherwise see that text land in one cell, or repeated
    # across the range, with status: ok. The refusal tells it to send the 2-D array.
    # One-line text into one cell stays literal, because commas and semicolons are
    # normal in prose. Strings starting with '=' are exempt (e.g. =PY code with
    # line breaks, or formulas using ';').
    s_strip = s.strip()
    if not s_strip or s_strip.startswith("="):
        return None
    if "\n" in s:
        return (
            f'values for {range_name} is one text string with line breaks, and a string is written as one value. '
            'To write rows, send a 2-D array with one inner array per row, e.g. [["Name", "Age"], ["Ann", 30]]; '
            'the range may be just its top-left cell. To keep line breaks inside one cell, send [["line 1\\nline 2"]].'
        )
    if not single_cell and ("," in s or ";" in s):
        return (
            f'values for {range_name} is one text string containing , or ;, and a string is written as one value, '
            'not split into cells. To write one value per cell, send a 2-D array with one inner array per row, '
            'e.g. [["Name", "Age", "Salary"]] for a 1×3 range. To repeat the same text in every cell, send it once '
            'per cell in the 2-D array.'
        )
    return None


# ── Manipulator ────────────────────────────────────────────────────────


class CellManipulator:
    """Manages data writing and style application to cells."""

    bridge: Any

    def __init__(self, bridge: Any) -> None:
        """
        Args:
            bridge: CalcBridge instance.
        """
        self.bridge = bridge
        self._dialects: FormulaDialects | None = None

    @property
    def dialects(self) -> FormulaDialects:
        if self._dialects is None:
            self._dialects = FormulaDialects(self.bridge.doc)
        return self._dialects

    def _resolve_range_sheet(self, range_str: str) -> tuple[Any, Any, Any]:
        cell_range = self.bridge.resolve_range_or_address(range_str)
        addr = _uno_range_address(cell_range)
        return cell_range, addr, self.bridge.doc.getSheets().getByIndex(addr.Sheet)

    # ── Internal helpers ───────────────────────────────────────────────

    def _raise_if_intersects_array(self, sheet: Any, start_col: int, start_row: int, end_col: int, end_row: int, allow_contained: bool = False) -> None:
        '''Raise CalcError if the range intersects an array formula.

        If allow_contained is True, it's permissible for the range to fully enclose an array formula
        (e.g., when clearing a large area). It's an error only if the array formula is partially outside the range.
        '''
        cursor = sheet.createCursor()
        cursor.gotoEndOfUsedArea(False)
        used = cursor.getRangeAddress()

        c1, r1 = min(end_col, used.EndColumn), min(end_row, used.EndRow)

        if start_col > c1 or start_row > r1:
            return

        # Only formula cells can belong to an array, so probe just those instead of every cell: a big clear stays cheap.
        formula_ranges = sheet.getCellRangeByPosition(start_col, start_row, c1, r1).queryContentCells(_CELL_FLAG_FORMULA)
        contained: list[Any] = []
        for fa in formula_ranges.getRangeAddresses():
            for r in range(fa.StartRow, fa.EndRow + 1):
                for c in range(fa.StartColumn, fa.EndColumn + 1):
                    if any(b.StartColumn <= c <= b.EndColumn and b.StartRow <= r <= b.EndRow for b in contained):
                        continue
                    existing = self._array_block(sheet, c, r)
                    if not existing:
                        continue
                    if allow_contained and existing.StartColumn >= start_col and existing.EndColumn <= end_col and existing.StartRow >= start_row and existing.EndRow <= end_row:
                        contained.append(existing)
                        continue

                    cell_name = format_address(c, r)
                    arr_name = format_range(existing.StartColumn, existing.StartRow, existing.EndColumn, existing.EndRow)
                    raise CalcError(f"{cell_name} is part of array {arr_name}; edit or clear the whole array.")

    def _apply_borders(self, obj: Any, mode: str, color: int) -> None:
        """Apply or clear borders on a cell or range using ``TableBorder2``."""
        import uno

        # Per-side TopBorder2/BottomBorder2/LeftBorder2/RightBorder2 on a range boxes every cell
        # (cellsuno.cxx comment above lcl_GetCellsPropertySet), whereas TableBorder2 separates the
        # outer frame from inner grid lines (ScHelperFunctions::FillBoxItems). Setting Is*LineValid=True
        # with an empty BorderLine2 removes that line.
        line = uno.createUnoStruct(
            "com.sun.star.table.BorderLine2",
            Color=color,
            OuterLineWidth=_BORDER_LINE_WIDTH,
            LineStyle=BorderLineStyle.SOLID,
        )
        empty = uno.createUnoStruct("com.sun.star.table.BorderLine2")
        outer = line if mode in ("all", "outline") else empty
        inner = line if mode in ("all", "inner") else empty
        tb = uno.createUnoStruct(
            "com.sun.star.table.TableBorder2",
            TopLine=outer,
            BottomLine=outer,
            LeftLine=outer,
            RightLine=outer,
            HorizontalLine=inner,
            VerticalLine=inner,
            IsTopLineValid=True,
            IsBottomLineValid=True,
            IsLeftLineValid=True,
            IsRightLineValid=True,
            IsHorizontalLineValid=True,
            IsVerticalLineValid=True,
        )
        obj.setPropertyValue("TableBorder2", tb)

    # ── Style operations ───────────────────────────────────────────────

    def set_cell_style(
        self,
        address_or_range: str,
        bold: bool | None = None,
        italic: bool | None = None,
        bg_color: int | None = None,
        font_color: int | None = None,
        font_size: float | None = None,
        h_align: str | None = None,
        v_align: str | None = None,
        wrap_text: bool | None = None,
        border_color: int | None = None,
        border_mode: str | None = None,
        number_format_key: int | None = None,
        column_width: int | None = None,
        row_height: int | None = None,
        autofit: str | None = None,
    ) -> None:
        """Apply style and layout to a cell or range.

        Args:
            address_or_range: Cell address, range, or named range (e.g. "A1", "A1:D10", or "Totals").
            bold: Bold flag.
            italic: Italic flag.
            bg_color: Background colour (RGB int).
            font_color: Font colour (RGB int).
            font_size: Font size (points).
            h_align: Horizontal alignment ("left", "center", "right", "justify").
            v_align: Vertical alignment ("top", "center", "bottom").
            wrap_text: Wrap text flag.
            border_color: Border colour (RGB int).
            border_mode: Border mode ("all", "outline", "inner", "none").
            number_format_key: Resolved Calc NumberFormat key.
            column_width: Column width in 1/100 mm.
            row_height: Row height in 1/100 mm.
            autofit: Autofit mode ("columns", "rows", "both").
        """
        target = self.bridge.resolve_range_or_address(address_or_range)
        if bold is not None:
            target.setPropertyValue("CharWeight", FontWeight.BOLD if bold else FontWeight.NORMAL)
        if italic is not None:
            target.setPropertyValue("CharPosture", ITALIC if italic else NONE)
        if bg_color is not None:
            target.setPropertyValue("CellBackColor", bg_color)
        if font_color is not None:
            target.setPropertyValue("CharColor", font_color)
        if font_size is not None:
            target.setPropertyValue("CharHeight", font_size)
        if h_align is not None:
            align_map: dict[str, Any] = {"left": LEFT, "center": CENTER, "right": RIGHT, "justify": BLOCK, "standard": STANDARD}
            if h_align.lower() in align_map:
                target.setPropertyValue("HoriJustify", align_map[h_align.lower()])
        if v_align is not None:
            vert = _VERT_JUSTIFY.get(v_align.lower())
            if vert is not None:
                target.setPropertyValue("VertJustify", vert)
        if wrap_text is not None:
            target.setPropertyValue("IsTextWrapped", wrap_text)
        if border_mode is not None:
            self._apply_borders(target, border_mode, 0 if border_color is None else border_color)
        if number_format_key is not None:
            target.setPropertyValue("NumberFormat", number_format_key)
        # Apply layout last so autofit measures the final font, wrap, and number format.
        # Setting Width/Height or OptimalWidth/OptimalHeight on the column/row collection
        # records an undo step; OptimalWidth=False has no effect (docuno.cxx
        # ScTableColumnsObj::setPropertyValue).
        if column_width is not None:
            target.getColumns().setPropertyValue("Width", column_width)
        elif autofit in ("columns", "both"):
            target.getColumns().setPropertyValue("OptimalWidth", True)
        if row_height is not None:
            target.getRows().setPropertyValue("Height", row_height)
        elif autofit in ("rows", "both"):
            target.getRows().setPropertyValue("OptimalHeight", True)
        log.info("%s style updated.", address_or_range.upper())

    def number_format_key(self, format_str: str) -> int:
        """Resolve a preset name or en-US format code to a document NumberFormat key."""
        import uno
        from com.sun.star.util import MalformedNumberFormatException

        doc = self.bridge.doc
        formats = doc.getNumberFormats()
        locale = resolve_document_locale(doc)
        preset = _NUMBER_FORMAT_PRESETS.get(format_str.strip().lower())
        if preset is not None:
            # Presets match LibreOffice's toolbar buttons (viewfunc.cxx ScViewFunc::SetNumberFormat);
            # getFormatIndex currency indices return legacy DM/F codes. "general" is a preset
            # because addNewConverted("General") throws when General is key 0 (numfmuno.cxx).
            return int(formats.getStandardFormat(preset, locale))
        en_us = uno.createUnoStruct("com.sun.star.lang.Locale", Language="en", Country="US", Variant="")
        try:
            # Format codes are parsed as en-US and converted to the document locale (matching oox
            # numberformatsbuffer.cxx lclCreateFormat) so "#,##0.00" is not garbled in e.g. de-DE.
            return int(formats.addNewConverted(format_str, en_us, locale))
        except MalformedNumberFormatException:
            raise CalcError(
                f"Invalid number_format {format_str!r}: use currency, percent, general, or an en-US code such as '#,##0.00'.",
                code="INVALID_PARAM",
            ) from None

    # ── Range operations ───────────────────────────────────────────────

    def clear_range(self, range_str: str) -> None:
        """Clear all content in a cell range.

        Args:
            range_str: Cell range (e.g. "A1:D10").
        """
        cell_range, addr, sheet = self._resolve_range_sheet(range_str)
        # clearContents on part of an array formula silently does nothing, so
        # refuse with a clear message unless the range covers the whole array.
        self._raise_if_intersects_array(sheet, addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow, allow_contained=True)
        # CellFlags: VALUE=1, DATETIME=2, STRING=4, FORMULA=16 -> 23
        cell_range.clearContents(23)
        log.info("Range %s cleared.", range_str.upper())

    def merge_cells(self, range_str: str, center: bool = True, *, unmerge: bool = False) -> MergeOutcome:
        """Merge or unmerge a cell range.

        Args:
            range_str: Cell range to merge (e.g. "A1:D1").
            center: Centre content after merging.
            unmerge: When true, split the range instead of merging (center is skipped).
        """
        cell_range = self.bridge.resolve_range_or_address(range_str)
        if unmerge:
            # merge(False) splits the range, so unmerge=True passes False.
            cell_range.merge(False)
            log.info("Range %s unmerged.", range_str.upper())
            return MergeOutcome(True, 0, [])

        addr = _uno_range_address(cell_range)
        sc, sr, ec, er = int(addr.StartColumn), int(addr.StartRow), int(addr.EndColumn), int(addr.EndRow)
        sheet = cell_range.getSpreadsheet()
        occupied = _occupied_cells(sheet, sc, sr, ec, er, skip=[(sc, sr, sc, sr)])

        # ScCellRangeObj::merge (sc/source/ui/unoobj/cellsuno.cxx) passes bContents=false
        # to ScDocFunc::MergeCells (sc/source/ui/docshell/docfunc.cxx), so non-top-left
        # cells keep their content (hidden, still counted by formulas).
        cell_range.merge(True)

        # ScCellRangeObj::merge ignores the bool returned by ScDocFunc::MergeCells when
        # the range overlaps an existing merge or is protected, so check the resulting
        # merged area on the top-left cell. getIsMerged() is not used because it returns
        # True when the range merely contains a smaller merge.
        cur = sheet.createCursorByRange(sheet.getCellRangeByPosition(sc, sr, sc, sr))
        cur.collapseToMergedArea()
        merged_addr = _uno_range_address(cur)
        if (
            int(merged_addr.StartColumn),
            int(merged_addr.StartRow),
            int(merged_addr.EndColumn),
            int(merged_addr.EndRow),
        ) != (sc, sr, ec, er):
            return MergeOutcome(False, 0, [])

        log.info("Range %s merged.", range_str.upper())
        if center:
            cell_range.setPropertyValue("HoriJustify", CENTER)
            cell_range.setPropertyValue("VertJustify", _VERT_JUSTIFY["center"])

        sheet_name = str(sheet.getName())
        hidden_cells = [qualify_a1(sheet_name, format_address(c, r)) for c, r in occupied[:_MAX_REPORTED_CELLS]]
        return MergeOutcome(True, len(occupied), hidden_cells)

    def plan_sort(self, range_str: str, keys: Sequence[SortKey]) -> SortPlan:
        """Resolve *range_str* and validate *keys* against its width before any sort runs."""
        cell_range = self.bridge.resolve_range_or_address(range_str)
        addr = _uno_range_address(cell_range)
        width = int(addr.EndColumn) - int(addr.StartColumn) + 1
        height = int(addr.EndRow) - int(addr.StartRow) + 1
        # The schema's minimum is not enforced and the tool cannot know the width, so both
        # bounds for every key are checked here before any undo group exists (a refusal must
        # not sort an earlier range or clear Redo).
        width_error = _sort_keys_width_error(range_str, keys, width)
        if width_error is not None:
            raise CalcError(width_error, code="INVALID_PARAM")
        return SortPlan(range_str, cell_range, height, keys)

    def range_sort(self, plan: SortPlan, has_header: bool) -> str:
        """Execute a ``SortPlan`` produced by ``plan_sort`` and report whether any row moved."""
        import uno

        range_str, cell_range, height, keys = plan
        first_data_row = 1 if has_header else 0
        key_cols = list(dict.fromkeys(col for col, _unused in keys))

        def key_columns() -> tuple[tuple[Any, ...], ...]:
            # Only the key columns, data rows only. With fewer than two data rows
            # nothing can move, and getCellRangeByPosition raises when top > bottom.
            if height - first_data_row < 2:
                return ()
            return tuple(
                tuple(cell_range.getCellRangeByPosition(col, first_data_row, col, height - 1).getDataArray())
                for col in key_cols
            )

        fields = []
        for col, asc in keys:
            sort_field = TableSortField()
            sort_field.Field = col
            sort_field.IsAscending = asc
            sort_field.IsCaseSensitive = False
            fields.append(sort_field)

        # Calc reads SortFields only as a typed []TableSortField sequence. PyUNO
        # turns a bare tuple into a []any, and Calc's extraction (cppu
        # _assignData, assign.hxx; ScSortDescriptor::FillSortParam, datauno.cxx)
        # rejects that without raising, so the sort would run with no key and
        # leave every row where it was while still returning success. Calc
        # accepts more than three keys because ScSortDescriptor::FillSortParam
        # (datauno.cxx) resizes maKeyState (tdf#105301), and it clears keys left
        # from an earlier sort on the sheet's DB range.
        # uno.Any is a pyuno helper; stubs/mypy do not export it as a module attribute.
        uno_any = getattr(uno, "Any")
        sort_desc = list(cell_range.createSortDescriptor())
        for p in sort_desc:
            if p.Name == "SortFields":
                p.Value = uno_any("[]com.sun.star.table.TableSortField", tuple(fields))
            elif p.Name == "ContainsHeader":
                p.Value = has_header

        before = key_columns()
        cell_range.sort(tuple(sort_desc))
        if key_columns() == before:
            log.info("Range %s: key columns unchanged by the sort (keys=%s); no rows moved.", range_str.upper(), keys)
            return _sort_unchanged_message(range_str, keys)
        log.info("Range %s sorted with keys=%s.", range_str.upper(), keys)
        return _sort_done_message(range_str, keys)

    def _make_number_formatter(self, doc: Any) -> Any:
        """Attach a NumberFormatter to *doc* once per write invocation.

        Chat tools pass a Layer-A guarded document. ``attachNumberFormatsSupplier``
        is called on the formatter (not through the doc proxy), so the proxy is
        not auto-unwrapped — hand it the raw UNO supplier or attach fails with
        an often-empty UNO message and ISO writes never run.
        """
        uno_ctx = unwrap_uno(get_ctx())
        smgr = uno_ctx.getServiceManager()
        formatter = smgr.createInstanceWithContext("com.sun.star.util.NumberFormatter", uno_ctx)
        formatter.attachNumberFormatsSupplier(unwrap_uno(doc))
        return formatter

    def _classify_write_cell(
        self,
        value: Any,
        formatter: Any,
        std_key: Any,
        *,
        elapsed_format_key: int | None = None,
        literal_text: bool = False,
        data_text: bool = False,
    ) -> _CellWrite:
        """Classify one write input into data/formula/meta for the ISO write path."""
        if value is None or value == "":
            return _CellWrite("", "", "empty", None, None)

        if literal_text:
            return _CellWrite(value if isinstance(value, str) else str(value), "", "forced_text", None, None)

        # bool is a subclass of int; store 1.0/0.0, not the text "True"/"False".
        if isinstance(value, bool):
            return _CellWrite(1.0 if value else 0.0, "", "number", None, None)

        if isinstance(value, (int, float)):
            return _CellWrite(value, "", "number", None, None)

        if not isinstance(value, str):
            return _CellWrite(str(value), "", "text", None, None)

        if data_text and value.startswith(("=", "'")):
            return _CellWrite(value, "", "forced_text", None, None)

        if value.startswith("="):
            # Repair literal \n in model-written =PY() code before writing to Calc.
            return _CellWrite("", repair_py_code_newlines(value), "formula", None, None)

        # Leading apostrophe forces literal text and leaves @ — the @ text format is what we want; restoring the snapshot would undo it.
        if value.startswith("'"):
            return _CellWrite(value[1:], "", "forced_text", None, None)

        stripped = value.strip()
        # Duration is WriterAgent arithmetic; Calc's formatter does not parse PT….
        if match_iso_duration(stripped) and elapsed_format_key is not None:
            try:
                serial = duration_serial_from_iso(stripped)
            except Exception as e:
                if is_disposal(e):
                    raise
                log.debug("Duration convert failed for %r: %s", stripped, e)
                return _CellWrite(value, "", "text", None, None)
            return _CellWrite(float(serial), "", "temporal", "duration", int(elapsed_format_key))

        input_category = match_iso_temporal(stripped)
        if input_category is not None and formatter is not None:
            try:
                detected_key = formatter.detectNumberFormat(std_key, stripped)
                serial = formatter.convertStringToNumber(std_key, stripped)
                return _CellWrite(float(serial), "", "temporal", input_category, int(detected_key))
            except Exception as e:
                if is_disposal(e):
                    raise
                # NotNumericException and peers → ordinary text fallback (S4).
                if "NotNumeric" not in type(e).__name__:
                    log.debug("ISO convert failed for %r: %s", stripped, e)
                return _CellWrite(value, "", "text", None, None)

        try:
            num = float(value)
            return _CellWrite(num, "", "number", None, None)
        except ValueError:
            return _CellWrite(value, "", "text", None, None)

    def _find_column_temporal_templates(
        self,
        sheet: Any,
        columns_needing_templates: dict[int, str],
        scan_start_row: int,
        categories: NumberFormatCategories,
    ) -> dict[int, int]:
        """Find inherited NumberFormat keys by the upward scan that copies a column's existing number format.

        Args:
            sheet: The active sheet.
            columns_needing_templates: {col_index: input_category} for columns needing template.
            scan_start_row: Row index to start scanning upward from (exclusive).
            categories: Shared NumberFormatCategories reader for the document.

        Returns:
            {col_index: inherited_format_key} for columns where a template was found.

        Compatibility uses the stricter template check in ``is_compatible_temporal_template``:
        date does not inherit datetime; clock time skips elapsed FormatString templates.
        """
        column_templates: dict[int, int] = {}
        no_format: tuple[str | None, str | None] = (None, None)
        for col, input_category in columns_needing_templates.items():
            start_r = scan_start_row - 1
            min_r = max(0, scan_start_row - 100)
            for r in range(start_r, min_r - 1, -1):
                # An unreadable cell reads as key 0 and is skipped like a General cell.
                key = uno_read(lambda: int(sheet.getCellByPosition(col, r).getPropertyValue("NumberFormat")), 0)
                if key == 0:
                    continue
                cat, format_code = uno_read(lambda: categories.lookup(key), no_format)
                if is_compatible_temporal_template(input_category, cat, format_code):
                    column_templates[col] = key
                    break
        return column_templates

    def _apply_temporal_format_runs(self, sheet: Any, start: tuple[int, int], decisions: Any) -> None:
        """Apply detected NumberFormat keys with one NumberFormat write per coalesced rectangle.

        Resolves empty-cell bridging per row, finds horizontal apply runs, then
        vertically merges identical ``(c0, c1, key)`` spans before one range set
        per rectangle.
        """
        start_col, start_row = start
        rects = coalesce_temporal_apply_rects(decisions)
        for r0, r1, c0, c1, key in rects:
            target = sheet.getCellRangeByPosition(start_col + c0, start_row + r0, start_col + c1, start_row + r1)
            target.setPropertyValue("NumberFormat", int(key))

    def _restore_number_formats(self, sheet: Any, cells: list[tuple[int, int, int]]) -> None:
        """Put snapshot keys back after a text commit (S29), one NumberFormat write per distinct key.

        Writing a string that Calc could parse as a number or date under the cell's format
        (08/05/2026, 1,234, 50%) rewrites that cell to the @ text format; a date column would
        keep a Text hole otherwise. Cells are grouped by key into one SheetCellRanges each so a
        3000-cell write is a few UNO calls, not two per cell.
        """
        if not cells:
            return
        import uno

        sheet_index = sheet.getRangeAddress().Sheet
        by_key: dict[int, list[Any]] = {}
        for col, row, key in cells:
            addr = uno.createUnoStruct(
                "com.sun.star.table.CellRangeAddress",
                Sheet=sheet_index,
                StartColumn=col,
                EndColumn=col,
                StartRow=row,
                EndRow=row,
            )
            by_key.setdefault(key, []).append(addr)
        for key, addrs in by_key.items():
            ranges = self.bridge.doc.createInstance("com.sun.star.sheet.SheetCellRanges")
            ranges.addRangeAddresses(tuple(addrs), False)
            ranges.setPropertyValue("NumberFormat", key)

    def _probe_array_formula(
        self,
        sheet: Any,
        formula: str,
        start: tuple[int, int],
        end: tuple[int, int],
        premeasured_array_size: tuple[int, int] | None = None,
    ) -> tuple[int, int]:
        """Measure an array formula and check target occupancy without committing.

        We use a hidden undo context, not lock(), because Calc still records API edits while
        XUndoManager is locked. This merges the probe's temporary actions cleanly without leaving
        a phantom Ctrl+Z step.
        """
        doc = self.bridge.doc

        was_modified = doc.isModified()
        undo_mgr = doc.getUndoManager() if hasattr(doc, "getUndoManager") else None

        was_possible = False
        hidden_context_entered = False

        if undo_mgr:
            was_possible = undo_mgr.isUndoPossible()
            try:
                undo_mgr.enterHiddenUndoContext()
                hidden_context_entered = True
            except Exception as e:
                if "EmptyUndoStackException" not in type(e).__name__:
                    raise
        try:
            c2, r2 = end
            if premeasured_array_size is not None:
                rows, cols = premeasured_array_size
            else:
                rows, cols = self._measure_array(sheet, formula, avoid_col=c2, avoid_start_row=start[1], avoid_end_row=r2)
            self._write_array_formula(sheet, formula, start, end, measure_only=True, premeasured_array_size=(rows, cols))
            return (rows, cols)
        finally:
            if hidden_context_entered and undo_mgr:
                quiet_probe_cleanup(undo_mgr.leaveUndoContext, "leaveUndoContext")
            elif undo_mgr and not was_possible:
                quiet_probe_cleanup(undo_mgr.clear, "clear")

            if not was_modified:
                quiet_probe_cleanup(lambda: doc.setModified(False), "setModified")

    def plan_writes(
        self,
        values: Sequence[Any],
        formatter: Any,
        std_key: Any,
        elapsed_format_key: int | None = None,
        *,
        literal_text: bool = False,
        data_text: bool = False,
    ) -> list[_CellWrite]:
        """Classify write inputs into data/formula/meta cell writes without UNO access."""
        return [
            self._classify_write_cell(
                v,
                formatter,
                std_key,
                elapsed_format_key=elapsed_format_key,
                literal_text=literal_text,
                data_text=data_text,
            )
            for v in values
        ]

    def commit(
        self,
        sheet: Any,
        start: tuple[int, int],
        end: tuple[int, int],
        cell_metas: list[_CellWrite],
        formats: Any,
        *,
        literal_text: bool = False,
        data_text: bool = False,
    ) -> _CommitResult:
        """Commit data, formulas, and text writes to the sheet and restore number format snapshots.

        The snapshot is one native format-group read of the box, taken only when the write has
        text or temporal cells; the restore is one NumberFormat write per distinct prior key.
        """
        data_array: list[list[Any]] = []
        formula_cells: list[tuple[int, int, str]] = []  # (col, row, formula)
        literal_text_cells: list[tuple[int, int, str]] = []
        s29_snapshots: list[tuple[int, int, int]] = []  # col, row, key
        dest_categories: dict[tuple[int, int], str | None] = {}
        categories = NumberFormatCategories(formats)
        cols_needing: dict[int, str] = {}

        num_cols = end[0] - start[0] + 1
        num_rows = end[1] - start[1] + 1
        total_cells = num_rows * num_cols

        needs_keys = any(m.kind in ("text", "temporal") for m in cell_metas)
        prior_keys: dict[tuple[int, int], int] = uno_read(lambda: _number_format_keys(sheet, start, end), {}) if needs_keys else {}

        cell_idx = 0
        for row in range(start[1], end[1] + 1):
            data_row: list[Any] = []
            for col in range(start[0], end[0] + 1):
                cell_write = cell_metas[cell_idx]
                data_val = cell_write.data
                formula = cell_write.formula
                if (literal_text or data_text) and cell_write.kind == "forced_text":
                    # setDataArray/SetString parses a leading '=' as a formula
                    # and float() would drop leading zeros. Commit text via setString.
                    literal_text_cells.append((col, row, str(data_val)))
                    data_val = ""
                if formula:
                    formula_cells.append((col, row, formula))
                data_row.append(data_val)
                if cell_write.kind == "text" or cell_write.kind == "temporal":
                    # A failed group read leaves prior_keys empty. Text cells with no known key
                    # are then not restored at all: writing General over a date column would be
                    # worse than the stray @ Calc leaves on a few parseable strings.
                    key = prior_keys.get((col, row))
                    if cell_write.kind == "text" and key is not None:
                        s29_snapshots.append((col, row, key))
                    if cell_write.kind == "temporal":
                        dest_cat = uno_read(lambda: categories.category(key if key is not None else 0), None)
                        dest_categories[(col, row)] = dest_cat
                        if dest_cat is None and col not in cols_needing:
                            cols_needing[col] = cell_write.input_category or ""
                cell_idx += 1
            data_array.append(data_row)

        cell_range = sheet.getCellRangeByPosition(start[0], start[1], end[0], end[1])

        # setDataArray undo does not clear previously-empty cells.
        # Above 2000 cells, speed wins over that undo quirk.
        if total_cells > 2000 or not _range_has_blank_cell(sheet, start, end):
            cell_range.setDataArray(tuple(tuple(r) for r in data_array))
        else:
            for r_idx, row in enumerate(range(start[1], end[1] + 1)):
                for c_idx, col in enumerate(range(start[0], end[0] + 1)):
                    val = data_array[r_idx][c_idx]
                    cell = sheet.getCellByPosition(col, row)
                    if isinstance(val, (int, float)):
                        cell.setValue(float(val))
                    else:
                        cell.setString(str(val))

        self._restore_number_formats(sheet, s29_snapshots)

        # Non-=PY formula errors are scanned post-write in _write_report so forward references evaluate first.
        # =PY() errors and #SPILL! still compare diagnostics before and after setFormula here.
        py_warning = ""
        for col, row, formula in formula_cells:
            cell = sheet.getCellByPosition(col, row)
            parts = py_formula_or_none(formula) if not py_warning else None
            prior = _latest_py_diagnostic(self.bridge.doc, parts) if parts is not None else None
            cell.setFormula(formula)
            if parts is not None and not py_warning:
                err = int(cell.Error)
                if err != 0:
                    detail = _py_run_detail(self.bridge.doc, parts, prior)
                    py_warning = f"; warning: formula gives Err:{err}{detail}"
                elif cell.getString() == "#SPILL!":
                    detail = _py_run_detail(self.bridge.doc, parts, prior)
                    py_warning = f"; warning: {format_address(col, row)} shows #SPILL!{detail}"

        for col, row, text_value in literal_text_cells:
            sheet.getCellByPosition(col, row).setString(text_value)

        return _CommitResult(data_array, dest_categories, categories, cols_needing, py_warning)

    def apply_temporal_formats(
        self,
        sheet: Any,
        start: tuple[int, int],
        range_str: str,
        cell_metas: list[_CellWrite],
        committed: _CommitResult,
    ) -> str:
        """Apply detected temporal formats with upward template inheritance and coalesced writes."""
        data_array = committed.data_array
        dest_categories = committed.dest_categories
        categories = committed.categories
        cols_needing = committed.cols_needing
        if not any(m.kind == "temporal" for m in cell_metas):
            return ""

        format_warning = ""
        num_cols = len(data_array[0]) if data_array else 0
        column_templates = self._find_column_temporal_templates(sheet, cols_needing, start[1], categories)

        # Build row decisions: None | "empty" | ("apply", key) | ("preserve", None)
        decisions: list[list[tuple[str, int | None] | str | None]] = []
        for r_idx, row_vals in enumerate(data_array):
            row = start[1] + r_idx
            row_dec: list[tuple[str, int | None] | str | None] = []
            for c_idx, data_val in enumerate(row_vals):
                col = start[0] + c_idx
                meta = cell_metas[r_idx * num_cols + c_idx]
                if meta.kind == "temporal":
                    dest_cat = dest_categories.get((col, row))
                    if should_preserve_temporal_format(meta.input_category or "", float(data_val), dest_cat):
                        row_dec.append(("preserve", None))
                    else:
                        apply_key = column_templates.get(col, meta.detected_key)
                        row_dec.append(("apply", apply_key))
                elif meta.kind == "empty":
                    row_dec.append("empty")
                else:
                    row_dec.append(None)
            decisions.append(row_dec)
        try:
            self._apply_temporal_format_runs(sheet, start, decisions)
        except Exception as e:
            if is_disposal(e):
                raise
            log.exception("Date/time format pass failed for range %s", range_str)
            # The warning counts cells that needed a new format, not cells whose format was kept.
            apply_n = sum(1 for row_dec in decisions for d in row_dec if isinstance(d, tuple) and d[0] == "apply")
            if apply_n:
                format_warning = f"; could not apply date/time formats to {apply_n} cells in {range_str}"
            else:
                format_warning = f"; date/time format pass failed for {range_str}"
        return format_warning

    @contextmanager
    def _write_errors(self, range_str: str) -> Generator[None, None, None]:
        """Shared by plan_write and commit_write so a failure in either is explained the same way."""
        try:
            yield
        except WriterAgentException:
            # A refusal (CalcError) or a bridge error such as a missing sheet
            # (UnoObjectError) keeps its own code and details; only unexpected
            # LibreOffice errors get the array probe and the CalcError text.
            raise
        except Exception as e:
            if is_disposal(e):
                raise
            # Check if this cell intersects an array formula
            try:
                _cell_range, addr, sheet = self._resolve_range_sheet(range_str)
                self._raise_if_intersects_array(sheet, addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow, allow_contained=False)
            except CalcError:
                raise
            except Exception:
                log.debug("array-formula intersection probe failed for %s", range_str, exc_info=True)

            # UNO often yields str(e) == ""; keep a usable message for the agent.
            msg = str(e) or getattr(e, "Message", None) or type(e).__name__
            log.exception("Range formula write failed for %s", range_str)
            raise CalcError(msg) from e

    def _refuse_py_inside_own_data(
        self,
        sheet: Any,
        start: tuple[int, int],
        end: tuple[int, int],
        values: list[Any],
    ) -> None:
        """Refuse writing a =PY formula onto a cell covered by its own data argument."""
        sheet_name = sheet.getName()
        width = end[0] - start[0] + 1
        for idx, value in enumerate(values):
            # Plain text is never a formula; skip before py_formula_or_none so
            # long string writes cannot hit PyFormula.parse's 8192-char @deal contract.
            if not isinstance(value, str) or not value.startswith("="):
                continue
            parts = py_formula_or_none(value)
            if parts is None:
                continue
            args = parts.data_args()
            col = start[0] + (idx % width)
            row = start[1] + (idx // width)
            target = format_address(col, row)
            for arg in args:
                if arg_covers_cell(arg, target, sheet_name=sheet_name):
                    dest = _free_cell_right_of(sheet, sheet_name, arg, args) if len(values) == 1 else None
                    details: dict[str, Any] = {"data_range": arg}
                    if dest:
                        details["suggested_call"] = suggest_call("write_range", range=[dest], values=value)
                    raise CalcError(
                        f"=PY in {target} reads {arg}, which includes {target} itself, "
                        "so Calc would show an error and the value there would be lost. "
                        f"Nothing was written; put the formula in an empty cell outside the data"
                        f"{', e.g. ' + dest if dest else ''}.",
                        code="PY_CIRCULAR",
                        details=details,
                    )

    def _refuse_py_array_block(self, sheet: Any, formula: str, start: tuple[int, int]) -> None:
        """Refuse a =PY array formula where =PY returns only its top-left value (before LibreOffice 26.8)."""
        if py_formula_or_none(formula) is None or accepts_returned_any(unwrap_uno(get_ctx())):
            return
        raise CalcError(
            "This LibreOffice (before 26.8) shows only the first value of a =PY result in every cell of an array formula. "
            "Write it to one cell without array=true; the result spills from there when Python auto-spill is on.",
            code="INVALID_PARAM",
            details={"suggested_call": suggest_call("write_range", range=[qualify_a1(sheet.getName(), format_address(*start))], values=formula)},
        )

    def plan_write(
        self,
        range_str: str,
        values: Any,
        array: bool | None = None,
        *,
        literal_text: bool = False,
        data_text: bool = False,
        check_cell_types: bool = False,
        premeasured_array_size: tuple[int, int] | None = None,
    ) -> WritePlan:
        """Resolve ``range_str`` once and decide what ``values`` means there without changing a cell.

        Raises ``CalcError`` for a shape or type that does not fit, so a caller writing several
        ranges can refuse before the first is touched.
        """
        with self._write_errors(range_str):
            # Handle empty values as a clear_range operation; resolve first so an unknown
            # range is refused during planning before any earlier range is cleared.
            if values is None or values == "" or values == [] or values == "[]" or values == "{}":
                self.bridge.resolve_range_or_address(range_str)
                return WritePlan("clear", range_str)

            cell_range = self.bridge.resolve_range_or_address(range_str)
            addr = _uno_range_address(cell_range)
            sheet = cell_range.getSpreadsheet()
            start = (addr.StartColumn, addr.StartRow)
            end = (addr.EndColumn, addr.EndRow)
            ref_pos = make_cell_address(addr.Sheet, addr.StartColumn, addr.StartRow)
            notes: list[Diagnostic] = []
            formulas_on = not literal_text and not data_text

            if formulas_on and isinstance(values, str) and values.startswith("="):
                values = self.dialects.to_api(values, ref_pos)

            # LibreOffice does not spill: setFormula on =SORT(FILTER(...)) shows only
            # the first value. A top-level dynamic-array function is entered with
            # setArrayFormula over its result size instead of being filled down;
            # =SUM(FILTER()) stays a scalar formula.
            if formulas_on and _requests_array_formula(values, array):
                # Repair literal \n in model-written =PY() code before probing or planning.
                values = repair_py_code_newlines(values)
                self._refuse_py_array_block(sheet, values, start)
                return WritePlan(
                    "array",
                    range_str,
                    sheet,
                    start,
                    end,
                    values,
                    array_size=self._probe_array_formula(sheet, values, start, end, premeasured_array_size),
                    notes=tuple(notes),
                )

            num_rows = addr.EndRow - addr.StartRow + 1
            num_cols = addr.EndColumn - addr.StartColumn + 1
            total = num_rows * num_cols
            _check_write_cap(total)

            # A JSON array string becomes the array; other text is refused when it
            # would be ambiguous as one value.
            if isinstance(values, str):
                parsed = _parse_json_array_string(values)
                if parsed is not None:
                    values = parsed
                else:
                    refusal = _delimited_text_message(range_str, values, single_cell=start == end)
                    if refusal:
                        raise CalcError(refusal, code="INVALID_PARAM")
            caller_list = isinstance(values, (list, tuple))

            rows_2d = isinstance(values, (list, tuple)) and bool(values) and isinstance(values[0], (list, tuple))
            r_cnt = 0
            c_cnt = 0
            bad_row: int | None = None
            if rows_2d:
                r_cnt, c_cnt = len(values), len(values[0])
                bad_row = _first_ragged_row(values)
                if total > 1 and (bad_row is not None or (r_cnt, c_cnt) != (num_rows, num_cols)):
                    raise CalcError(_grid_mismatch_message(range_str, values, num_rows, num_cols, start, bad_row))
            elif isinstance(values, (list, tuple)) and len(values) != total:
                raise CalcError(_values_length_mismatch_message(range_str, len(values), total, num_rows, num_cols))

            if check_cell_types and isinstance(values, (list, tuple)):
                err = _write_values_grid_cell_error(list(values))
                if err:
                    raise CalcError(err)

            expanded = False
            if rows_2d:
                if total == 1:
                    if bad_row is not None:
                        raise CalcError(_grid_mismatch_message(range_str, values, 1, 1, start, bad_row))
                    end = (start[0] + c_cnt - 1, start[1] + r_cnt - 1)
                    _check_write_cap(r_cnt * c_cnt)
                    range_str = format_range(start[0], start[1], end[0], end[1])
                    expanded = True
                values = [v for row in values for v in row]
            elif not isinstance(values, (list, tuple)):
                # One formula written to a multi-cell range is filled like Calc's
                # fill-down: relative A1 refs shift per cell (expand_single_formula), and
                # =PY formulas follow the direction-aware span policy in formula_fill.
                if formulas_on and isinstance(values, str) and values.startswith("=") and total > 1:
                    values = expand_single_formula(values, num_rows, num_cols)
                else:
                    values = [values] * total

            if formulas_on and caller_list:
                values = _calc_grammar_in_values(self.dialects, list(values), ref_pos)
                rows = end[1] - start[1] + 1
                cols = end[0] - start[0] + 1
                sheet_name = sheet.getName()
                for col_offset, head in repeated_formula_columns(values, start[1], rows, cols):
                    col_idx = start[0] + col_offset
                    col_letter = index_to_column(col_idx)
                    col_range = format_range(col_idx, start[1], col_idx, end[1])
                    target_range = qualify_a1(sheet_name, col_range) if isinstance(sheet_name, str) and sheet_name else col_range
                    notes.append({
                        "code": "REPEATED_FORMULA",
                        "message": (
                            f"Column {col_letter} repeated {head} on every row, so every row stayed pinned to {head}. "
                            "If you meant fill-down, resend as a single formula string (or use $ to pin intentionally)."
                        ),
                        "suggested_call": suggest_call("write_range", range=[target_range], values=head),
                    })

            if formulas_on:
                self._refuse_py_inside_own_data(sheet, start, end, list(values))

            return WritePlan(
                "cells",
                range_str,
                sheet,
                start,
                end,
                list(values),
                expanded=expanded,
                literal_text=literal_text,
                data_text=data_text,
                notes=tuple(notes),
            )

    def commit_write(self, plan: WritePlan) -> dict[str, Any]:
        """Execute a ``WritePlan`` produced by ``plan_write``."""
        with self._write_errors(plan.range_str):
            if plan.kind == "clear":
                self.clear_range(plan.range_str)
                return {"message": f"Range {plan.range_str} cleared."}
            if plan.kind == "array":
                return _with_notes(
                    self._write_array_formula(
                        plan.sheet,
                        plan.values,
                        plan.start,
                        plan.end,
                        premeasured_array_size=plan.array_size,
                    ),
                    plan.notes,
                )

            range_str = plan.range_str
            sheet = plan.sheet
            start = plan.start
            end = plan.end
            values = plan.values
            literal_text = plan.literal_text
            data_text = plan.data_text

            expand_overwrite_warning: str | None = None
            if plan.expanded:
                # One getFormulaArray read replaces a UNO round trip per cell.
                non_empty_count = len(_occupied_cells(sheet, start[0], start[1], end[0], end[1]))
                if non_empty_count > 0:
                    expand_overwrite_warning = f"overwrote {non_empty_count} non-empty cell(s)"

            doc = self.bridge.doc
            formats = doc.getNumberFormats()
            locale = resolve_document_locale(doc)
            std_key = formats.getStandardIndex(locale)
            # OCR grids must not be scanned as dates or numbers. literal_text
            # commits with setString below.
            if literal_text:
                needs_formatter = needs_duration = False
            else:
                cands = [v.strip() for v in values if isinstance(v, str) and not v.startswith(("=", "'"))]
                needs_formatter = any(match_iso_temporal(v) for v in cands)
                needs_duration = any(match_iso_duration(v) for v in cands)
            formatter = self._make_number_formatter(doc) if needs_formatter else None
            elapsed_key = elapsed_format_key(formats, locale) if needs_duration else None

            cell_metas = self.plan_writes(
                values,
                formatter,
                std_key,
                elapsed_format_key=elapsed_key,
                literal_text=literal_text,
                data_text=data_text,
            )

            committed = self.commit(
                sheet,
                start,
                end,
                cell_metas,
                formats,
                literal_text=literal_text,
                data_text=data_text,
            )

            format_warning = self.apply_temporal_formats(
                sheet,
                start,
                range_str,
                cell_metas,
                committed,
            )

            msg = _write_summary(
                range_str,
                cell_metas,
                format_warning + committed.py_warning,
                expand_overwrite_warning,
            )

            log.info("%s", msg)
            return _with_notes(
                {
                    "message": msg,
                    **_write_report(
                        sheet,
                        start,
                        end,
                        values,
                        cell_metas,
                        scan_text=not literal_text and not data_text,
                    ),
                },
                plan.notes,
            )

    def write_formula_range(
        self,
        range_str: str,
        formula_or_values: Any,
        array: bool | None = None,
        *,
        literal_text: bool = False,
        data_text: bool = False,
    ) -> dict[str, Any]:
        """Write formula(s) or value(s) to a cell range.

        ISO date/time strings matching the wire gate become Calc serials with
        category-compatible NumberFormat preservation (see docs/calc/date-time-handling.md).

        Args:
            range_str: Cell range (e.g. "A1:A10", "B2:D2").
            formula_or_values: Single formula/value for all cells, or a
                list/array of values for each cell. A single ordinary
                formula into a 1×N or N×1 range fill-down/across with
                relative A1 adjust. ``=PY`` uses a span-peek (multi-row
                DataRange stays verbatim). A 2-D range plus one formula
                is an error — pass an explicit array instead.
                Excel-style formula text (commas, Sheet!A1, {1,2;3,4}) is
                translated to Calc's API grammar by ``formula_dialect``, and
                list columns repeating one row-aligned formula stay as sent with a
                ``REPEATED_FORMULA`` warning.
                A top-level FILTER/SORT/UNIQUE (or ``array=True``) is
                entered with ``setArrayFormula`` so the whole result shows.
            array: Optional override. ``True`` forces an array formula
                (LET/XLOOKUP); ``False`` forces scalar ``setFormula``.
            literal_text: When true, every non-empty value is stored with
                ``setString`` (text). Skips formula detection and ``float()``.
            data_text: Stores strings starting with '=' or '\'' verbatim with
                ``setString``; everything else is classified as usual.

        Returns:
            A dict with ``message``, plus optional ``error_cells``,
            ``text_that_looks_numeric``, ``apostrophe_removed``, and
            ``warnings`` when applicable; an array formula adds
            ``array_range`` / ``rows`` / ``cols`` / ``preview``.
        """
        return self.commit_write(
            self.plan_write(
                range_str,
                formula_or_values,
                array,
                literal_text=literal_text,
                data_text=data_text,
            )
        )

    @staticmethod
    def _array_probe_origin(used: Any, avoid_col: int, avoid_start_row: int, avoid_end_row: int) -> tuple[int, int]:
        """``(column, first_row)`` for the two-cell ``ROWS``/``COLUMNS`` probe.

        The probe sits two columns to the right of the used area and the
        write target, clamped to XFD (column 16383). When the used area
        already reaches XFD, place the probe two rows below the used
        rectangle so the probe does not overwrite the user's last column.
        """
        col = min(max(used.EndColumn, avoid_col) + 2, MAX_COL)
        row = 0
        overlaps_used = col <= used.EndColumn and used.StartRow <= 1 and used.EndRow >= 0
        overlaps_target = col <= avoid_col and avoid_start_row <= 1 and avoid_end_row >= 0
        if overlaps_used or overlaps_target:
            row = max(used.EndRow, avoid_end_row) + 1
            if row + 1 > MAX_ROW:
                raise CalcError("Cannot measure the array formula: no free cells below the used area in column XFD.")
        return col, row

    def _measure_array(self, sheet: Any, formula: str, *, avoid_col: int, avoid_start_row: int = 0, avoid_end_row: int = 0) -> tuple[int, int]:
        """``(rows, columns)`` of *formula*'s result, measured by LibreOffice.

        ``=ROWS(expr)`` and ``=COLUMNS(expr)`` are entered as array formulas
        in two scratch cells to the right of the used area (and the target),
        read, then cleared. When that column would fall inside the used
        area (sheet already at XFD), the scratch cells move below it.
        A single value measures 1 x 1; raises ``CalcError`` when the formula itself fails.
        """
        cursor = sheet.createCursor()
        cursor.gotoEndOfUsedArea(False)
        used = cursor.getRangeAddress()
        col, row0 = self._array_probe_origin(used, avoid_col, avoid_start_row, avoid_end_row)
        expr = formula[1:] if formula.startswith("=") else formula
        sizes: list[int] = []
        try:
            for row, fn in ((row0, "ROWS"), (row0 + 1, "COLUMNS")):
                probe = sheet.getCellRangeByPosition(col, row, col, row)
                probe.setArrayFormula("=%s(%s)" % (fn, expr))
                cell = sheet.getCellByPosition(col, row)
                if int(cell.Error):
                    # ROWS and COLUMNS reject a plain value (ScInterpreter::ScRows / ScColumns,
                    # sc/source/core/tool/interpr1.cxx), so evaluate the formula alone: without an
                    # error it returned one value. The parentheses keep a =PY here from being
                    # located as a formula cell that spills beside the scratch cell.
                    probe.setArrayFormula("=(%s)" % expr)
                    err = int(cell.Error)
                    if not err:
                        return 1, 1
                    shown = cell.getString()
                    # #NAME? (525) means an unknown function, not an empty FILTER.
                    if err == 525:
                        hint = " — unknown function name (this LibreOffice may lack TAKE/VSTACK etc.)"
                    else:
                        hint = " — e.g. FILTER with no matching row gives #CALC!"
                    raise CalcError("The formula returns an error (%s, code %d)%s." % (shown or "error", err, hint))
                sizes.append(int(round(cell.getValue())))
        finally:
            for row in (row0, row0 + 1):
                best_effort(lambda: _clear_array_range(sheet.getCellRangeByPosition(col, row, col, row)), "clear measurement array cell")
        return sizes[0], sizes[1]

    @staticmethod
    def _array_block(sheet: Any, col: int, row: int) -> Any | None:
        """Range address of the array formula covering (*col*, *row*), or None."""

        def block() -> Any | None:
            cursor = sheet.createCursorByRange(sheet.getCellRangeByPosition(col, row, col, row))
            cursor.collapseToCurrentArray()
            addr = cursor.getRangeAddress()
            rng = sheet.getCellRangeByPosition(addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow)
            return addr if rng.getArrayFormula() else None

        return uno_read(block, None)

    def _write_array_formula(self, sheet: Any, formula: str, start: tuple[int, int], end: tuple[int, int], measure_only: bool = False, premeasured_array_size: tuple[int, int] | None = None) -> dict[str, Any]:
        """Enter *formula* as an array formula so its whole result shows.

        From a single cell, the result range is sized from the result and
        must be empty except the origin (an array formula already anchored
        there is replaced). On an explicit range, that range is used as
        given; the answer says if the result is larger (rows cut) or
        smaller (a single row or column repeats, otherwise #N/A).
        """
        c1, r1 = start
        c2, r2 = end
        explicit = (c1, r1) != (c2, r2)
        if explicit:
            height, width = r2 - r1 + 1, c2 - c1 + 1
            if height * width > MAX_ARRAY_CELLS:
                raise CalcError("The target range is %d x %d cells, over the %d-cell limit." % (height, width, MAX_ARRAY_CELLS))

        if premeasured_array_size:
            rows, cols = premeasured_array_size
        else:
            rows, cols = self._measure_array(sheet, formula, avoid_col=c2, avoid_start_row=r1, avoid_end_row=r2)

        target, notes = plan_target(c1, r1, c2, r2, rows, cols)

        block_name = format_range(target.left, target.top, target.right, target.bottom)

        # Rewriting the same array formula in place is an update.
        # Cells inside the array being replaced are not occupied, and the
        # clear runs only after the new target is accepted. Clearing first
        # with setArrayFormula("") deletes the old array before an occupied
        # target cell can refuse the write.
        existing = self._array_block(sheet, c1, r1)
        replaced_box: tuple[int, int, int, int] | None = None
        if existing is not None and (existing.StartColumn, existing.StartRow) == (c1, r1):
            replaced_box = (int(existing.StartColumn), int(existing.StartRow), int(existing.EndColumn), int(existing.EndRow))

        refuse_occupied_cells(
            sheet,
            target.left,
            target.top,
            target.right,
            target.bottom,
            skip=[(c1, r1, c1, r1)] + ([replaced_box] if replaced_box is not None else []),
        )

        if measure_only:
            return {}

        if replaced_box is not None:
            sheet.getCellRangeByPosition(*replaced_box).setArrayFormula("")

        rng = sheet.getCellRangeByPosition(*target)
        rng.setArrayFormula(formula)
        anchor = sheet.getCellByPosition(target.left, target.top)
        if int(anchor.Error):
            best_effort(lambda: _clear_array_range(rng), "clear rejected array formula")
            raise CalcError("The formula returns an error (%s, code %d)." % (anchor.getString() or "error", int(anchor.Error)))
        preview_max_c = min(target.right, target.left + _ARRAY_PREVIEW_COLS - 1)
        preview_max_r = min(target.bottom, target.top + _ARRAY_PREVIEW_ROWS - 1)
        preview_rng = sheet.getCellRangeByPosition(target.left, target.top, preview_max_c, preview_max_r)
        preview = [list(r) for r in preview_rng.getDataArray()]
        payload: dict[str, Any] = {"message": "Array formula entered on %s (%d x %d)." % (block_name, rows, cols), "array_range": block_name, "rows": rows, "cols": cols, "preview": preview}
        if rows > _ARRAY_PREVIEW_ROWS or cols > _ARRAY_PREVIEW_COLS:
            payload["preview_truncated"] = True
        if notes:
            payload["warning"] = " ".join(notes)
        log.info("%s", payload["message"])
        return payload

    def copy_formula_range(self, source_str: str, dest_str: str) -> dict[str, Any]:
        """Copy formulas and values from *source_str* onto *dest_str* start.

        Uses sheet ``copyRange`` (``XCellRangeMovement``) so the paste matches
        Calc: relative refs shift by the dest offset, ``$`` anchors stay.
        Dest is the top-left (or a range whose start is used); size is the
        source extent.
        """
        try:
            source_obj = self.bridge.resolve_range_or_address(source_str)
            source_ra = _uno_range_address(source_obj)
            _dest_obj, dest_ra, dest_sheet = self._resolve_range_sheet(dest_str)
            rows = int(source_ra.EndRow) - int(source_ra.StartRow) + 1
            cols = int(source_ra.EndColumn) - int(source_ra.StartColumn) + 1

            dest_cell = make_cell_address(dest_ra.Sheet, dest_ra.StartColumn, dest_ra.StartRow)

            dest_sheet.copyRange(dest_cell, source_ra)

            msg = f"Copied {rows}×{cols} from {source_str} onto {dest_str}."
            log.info("%s", msg)
            return {"message": msg, "rows_copied": rows, "cols_copied": cols}
        except Exception as e:
            if is_disposal(e) or isinstance(e, WriterAgentException):
                raise
            msg = str(e) or getattr(e, "Message", None) or type(e).__name__
            log.exception("Range copy failed from %s onto %s", source_str, dest_str)
            raise CalcError(msg) from e

    # ── Structure operations ───────────────────────────────────────────

    def _structure_mutate(
        self,
        structure_type: str,
        start: Any,
        count: int,
        *,
        insert: bool,
        sheet: Any | None = None,
    ) -> dict[str, Any]:
        # The tool schema and the tool's assert restrict structure_type to "rows" or "columns".
        target = sheet if sheet is not None else self.bridge.get_active_sheet()
        assert target is not None
        name = str(target.getName())
        index = _structure_index(structure_type, start)
        if structure_type == "rows":
            rows = target.getRows()
            if insert:
                rows.insertByIndex(index, count)
            else:
                rows.removeByIndex(index, count)
            kind, where = "row", start
        else:
            where = str(start).upper()
            kind = "column"
            cols = target.getColumns()
            if insert:
                cols.insertByIndex(index, count)
            else:
                cols.removeByIndex(index, count)
        if insert:
            msg = f"{count} {kind}(s) inserted starting at {kind} {where} on sheet {name}."
            log.info("%s", msg)
            return {"message": msg, "sheet": name}
        msg = f"{count} {kind}(s) deleted starting from {kind} {where} on sheet {name}."
        log.info("%s", msg)
        return {"message": msg, "sheet": name}

    def delete_structure(
        self,
        structure_type: str,
        start: Any,
        count: int = 1,
        *,
        sheet: Any | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Delete rows or columns, refusing when formulas or named ranges would turn into ``#REF!`` unless *confirm* is true."""
        target = sheet if sheet is not None else self.bridge.get_active_sheet()
        assert target is not None
        name = str(target.getName())
        index = _structure_index(structure_type, start)
        rows = structure_type == "rows"
        doc = self.bridge.doc
        before = _ref_formula_cells(doc)
        names_before = _ref_named_ranges(doc)
        title = _("WriterAgent: Delete rows or columns")
        # UNO has no dry run for row/column removal (sc/source/ui/unoobj/cellsuno.cxx),
        # and predicting breaks across relative refs, names, 3-D refs, and INDIRECT
        # is far more code than deleting inside an undo group and rolling back.
        group = UndoStepOrRollback(doc, title)
        result: dict[str, Any] = {}
        broken: list[tuple[str, int, int]] = []
        broken_names: list[tuple[str, str]] = []
        try:
            with group:
                result = self._structure_mutate(structure_type, start, count, insert=False, sheet=target)
                after = _ref_formula_cells(doc)
                # Subtract shifted pre-existing #REF! cells so they are not reported as newly broken.
                broken = sorted(
                    after - _surviving_ref_cells(before, name, rows, index, count),
                    key=lambda t: (t[0], t[2], t[1]),
                )
                broken_names = sorted(_ref_named_ranges(doc) - names_before)
                if (broken or broken_names) and not confirm:
                    raise _DeleteHeldBack()
        except _DeleteHeldBack:
            pass

        if broken or broken_names:
            n = len(broken) + len(broken_names)
            sample = [qualify_a1(s, format_address(c, r)) for s, c, r in broken[:_MAX_REPORTED_CELLS]]
            details: dict[str, Any] = {"sheet": name, "refs_broken": n, "broken_cells": sample}
            if broken_names:
                details["broken_names"] = [
                    {"name": nm, "scope": sc} for sc, nm in broken_names[:_MAX_REPORTED_CELLS]
                ]
            what = _broken_refs_summary(len(broken), sample, broken_names)
            if not confirm:
                if group.rolled_back:
                    um = doc.getUndoManager()
                    # Without clearing this redo entry, Ctrl+Y would perform the deletion that
                    # was just refused.
                    if um is not None and tuple(um.getAllRedoActionTitles() or ())[:1] == (title,):
                        best_effort(um.clearRedo, "clearRedo after held-back delete failed")
                    raise CalcError(
                        f"Deleting {count} {structure_type} on sheet {name} would turn {what} into #REF!. "
                        "Nothing was deleted. Tell the user which formulas or named ranges would break; "
                        "call delete_structure again with confirm=true only if they still want it.",
                        code="REFS_WOULD_BREAK",
                        details=details,
                    )
                if group.rollback_error is not None:
                    raise CalcError(
                        f"Rollback failed after refusing to delete {count} {structure_type} on sheet {name}; "
                        f"the deletion may remain in the document ({group.rollback_error}).",
                        code="ROLLBACK_FAILED",
                        details=details,
                    )
            result.update(details)
            undo_note = (
                "One Ctrl+Z undoes the deletion."
                if group.entered
                else "Undo was unavailable, so the deletion could not be held for confirmation."
            )
            result["warnings"] = [
                Diagnostic(
                    code="REFS_BROKEN",
                    message=f"{what} now show #REF!. {undo_note}",
                )
            ]
        return result

    def insert_structure(
        self, structure_type: str, start: Any, count: int = 1, *, sheet: Any | None = None
    ) -> dict[str, Any]:
        """Insert empty rows or columns."""
        return self._structure_mutate(structure_type, start, count, insert=True, sheet=sheet)
