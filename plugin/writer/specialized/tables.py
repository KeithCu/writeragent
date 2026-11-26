# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Table tools: Writer XTextTable and Draw/Impress TableShape (same names, different UNO).

Writer: named text tables (table_list / getCellByName), plus Writer-only cell merge/split
(table_merge_cells / table_split_cell). Draw: TableShape on a page (page + shape index, or
shape.Name); Draw does not merge or split cells. Writer cell text is replaced in place
(keeping run formatting) and is recorded as a tracked change while Track Changes or review
mode is on.
"""
import itertools
import logging
from typing import Any, Callable

from plugin.doc.compound_undo import UndoStepOrRollback
from plugin.doc.text_helpers import (
    FAILED,
    KeepViewSelection,
    TrackedDeletions,
    get_string_without_tracked_deletions,
    iter_elements,
)
from plugin.framework.doc_type import is_draw
from plugin.framework.errors import ToolExecutionError, format_error_payload, is_disposal, make_tool_error, uno_read
from plugin.framework.i18n import _
from plugin.framework.tool_schema import page_index_param
from plugin.framework.uno_context import enum_has_more, execute_dispatch, resolve_uno_context, uno_same
from plugin.writer.edit_review import annotate_review_status, flag_tracked
from plugin.writer.html_import import (
    non_text_portion_type,
    replace_preserving_format,
    run_writer_mutation_with_optional_review,
)
from plugin.writer.record_changes import is_recording_changes
from plugin.writer.review_scan import scan_redlines

from ..range_copy import cells_all_tracked_deleted, supports_service, writer_cell_position  # LibrePy-shipped; do not invert
from ..specialized_base import ToolWriterTableBase, enum_param
from ..table_helpers import check_grid, check_table_band, edit_table_band, fill_table_cells, table_dims
from ..table_nesting import (
    cell_hosted_table_names,
    cell_plain_siblings,
    set_host_paragraphs,
)  # LibrePy-shipped; do not invert
from ..target_resolver import request_selection_range

log = logging.getLogger("writeragent.writer.specialized.tables")

# The zero-width joiner Writer drops into an empty row to anchor its tracked deletion.
_EMPTY_ROW_ANCHOR = "\u200d"
# LibreOffice relative column-separator total (sw/inc/fmtclds.hxx / TableColumnSeparators).
_UNO_TABLE_COLUMN_SUM = 10000
_PAGE_PROPERTY: dict[str, Any] = page_index_param("Draw/Impress only.")
# (cell name or None for a box covered by a vertical merge, left edge, right edge)
_Box = tuple[str | None, int, int]
_TEXT_CONTENT = "text, or a pending tracked deletion"
_CELL_DELETION_REFUSAL = (
    "Cell %s of table '%s' holds a pending tracked deletion. With Track Changes off, rewriting it "
    "would put the new text inside that deletion or remove it, so the cell was not changed. Tell the "
    "user; with Track Changes on the rewrite is recorded as a tracked change. Do not accept or reject "
    "the deletion yourself."
)
_NOT_RECORDED_HELP = (
    "Do not retry; tell the user. If the table or its text is a pending tracked insertion, "
    "the user must accept or reject that first (table_set_cell can still change its text); "
    "if the table is in a protected section (Format > Sections) or a protected merged cell "
    "reaches in (Table > Unprotect Cells), the user must unprotect it."
)


def _cursor_refused(name: str) -> ToolExecutionError:
    return ToolExecutionError(
        "LibreOffice could not place the cursor in table '%s' (it may be in a hidden or protected section), "
        "so deleting from it cannot be recorded; it was left in place. Ask the user to show or unprotect it, "
        "then retry." % name,
        code="TABLE_DELETE_NOT_TRACKABLE",
    )


def _tables(doc: Any) -> Any:
    """The document's text-table collection (XNameAccess)."""
    if not hasattr(doc, "getTextTables"):
        raise ValueError("This document has no text tables.")
    return doc.getTextTables()


def _get_table(doc: Any, name: str) -> Any:
    """Table by name, or a ValueError listing the available names."""
    tables = _tables(doc)
    names = list(tables.getElementNames())
    if not name or not tables.hasByName(name):
        listing = ", ".join(names) if names else "none"
        raise ValueError("No table named '%s'. Open tables (call table_list): %s." % (name, listing))
    return tables.getByName(name)


def _require_table(doc: Any, name: str) -> Any:
    """Table by name, requiring a non-empty name."""
    if not name:
        raise ValueError("name is required.")
    return _get_table(doc, name)


def _draw_ok(entry: dict[str, Any], name: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": "ok",
        "table_name": entry.get("name") or name,
        "page": entry.get("page"),
        "index": entry.get("index"),
        **extra,
    }


def _raise_draw_result(result: dict[str, Any], default_message: str) -> dict[str, Any]:
    if result.get("status") != "ok":
        return make_tool_error(
            str(result.get("message") or default_message),
            code=str(result.get("code") or "TOOL_EXECUTION_ERROR"),
        )
    return result


def _resolve_cell_name(raw: str, names: list[str]) -> str | None:
    """Match a user-supplied cell address against the table's REAL cell names.

    Exact match first, then the uppercased form — NEVER a blind upper rewrite: on a >26-column
    table 'a1' (Writer's real name for column 27) and 'A1' are DIFFERENT cells, and upping the
    input would silently write the wrong one."""
    if raw in names:
        return raw
    up = raw.upper()
    return up if up in names else None


def _writer_named_cells(table: Any) -> list[str]:
    """Address list from ``getCellNames()`` — not ``range(cols)`` from ``getColumns()``.

    ``getColumns()`` is the first row's box count (``SwXTableColumns::getCount``;
    the ``IsTableComplex`` guard above that return is commented out). After
    merging A1:D1 on a 5×4 table it reports 1, so a ``range(cols)`` walk would skip D2.
    SwXTextTable::getCellNames (sw/source/core/unocore/unotbl.cxx) returns an
    empty list itself for a table without a format and does not throw. Any other
    failure is raised so a delete guard does not treat it as "no nested tables".
    """
    return list(table.getCellNames() or ())


def _writer_grid_ok(table: Any, name: str, **extra: Any) -> dict[str, Any]:
    """Success payload after a Writer merge/split: fresh rows, cols and cell_names.

    The names are re-read because a merge or split renumbers cells per row; the model must
    address the next edit by these names, not the ones it read before.
    """
    rows, cols = table_dims(table)
    return {
        "status": "ok",
        "table_name": name,
        **extra,
        "rows": rows,
        "cols": cols,
        "cell_names": _writer_named_cells(table),
    }


def _resolve_writer_cells(doc: Any, name: str, raw_cells: list[str]) -> tuple[Any, list[str]]:
    """Resolve a Writer table and A1-style cell names against ``getCellNames()``."""
    table = _require_table(doc, name)
    names = _writer_named_cells(table)
    resolved: list[str] = []
    for raw in raw_cells:
        match = _resolve_cell_name(raw, names)
        if match is None:
            raise ValueError(_unknown_cell_message(raw, name, names))
        resolved.append(match)
    return table, resolved


def _unknown_cell_message(cell_raw: str, table_name: str, names: list[str]) -> str:
    if not names:
        sample = "none"
    elif len(names) <= 8:
        sample = ", ".join(names)
    else:
        sample = "%s, …, %s" % (", ".join(names[:8]), names[-1])
    return "Cell '%s' not in table '%s'. Its cells are: %s." % (
        cell_raw,
        table_name,
        sample,
    )


def _not_nested() -> dict[str, Any]:
    return {"is_nested": False, "parent_table": None, "parent_cell": None}


def _writer_nesting(
    doc: Any,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, list[str]]], dict[str, int]]:
    """One pass: child nesting, parent hosted-in-cells, and ``len(getCellNames())``.

    Direct parent only — a grandchild reports the mid table, not the outer.
    ``cell_count`` is counted here so ``table_list`` does not walk names twice.
    """
    tables = _tables(doc)
    names = list(tables.getElementNames())
    child_nesting: dict[str, dict[str, Any]] = {}
    hosted: dict[str, dict[str, list[str]]] = {}
    cell_counts: dict[str, int] = {}

    for parent_name in names:
        parent = uno_read(lambda: tables.getByName(parent_name), FAILED)
        cell_names = FAILED if parent is FAILED else uno_read(lambda: parent.getCellNames(), FAILED)
        if cell_names is FAILED:
            continue
        cell_counts[parent_name] = len(cell_names)
        for cell_name in cell_names:
            cell = uno_read(lambda: parent.getCellByName(cell_name), FAILED)
            if cell is FAILED:
                continue
            children = cell_hosted_table_names(cell)
            if not children:
                continue
            hosted.setdefault(parent_name, {})[cell_name] = children
            for child_name in children:
                if child_name == parent_name:
                    continue
                child_nesting[child_name] = {
                    "is_nested": True,
                    "parent_table": parent_name,
                    "parent_cell": cell_name,
                }

    for name in names:
        child_nesting.setdefault(name, _not_nested())
    return child_nesting, hosted, cell_counts


def _table_parent(doc: Any, table: Any) -> dict[str, Any] | None:
    """Nesting from the anchor XText: ``_not_nested()`` when it is the body.

    None means the anchor lives in another XText (cell, frame, header) or cannot be
    read, so the caller keeps the cell walk. PyUNO hands out distinct wrappers for
    one UNO object, so compare with ``uno_same``, never ``is``.
    """
    def probe() -> dict[str, Any] | None:
        anchor_text = table.getAnchor().getText()
        return _not_nested() if uno_same(anchor_text, doc.getText()) else None
    return uno_read(probe, None)


def _nesting_for(doc: Any, table: Any, name: str) -> dict[str, Any]:
    """Direct-parent nesting dict for one table (full cell walk only when nested)."""
    parent = _table_parent(doc, table)
    if parent is not None:
        return parent
    return _writer_nesting(doc)[0].get(name, _not_nested())


def _delete_redline_count(doc: Any) -> int | None:
    """Count Delete redlines in *doc*, or None when the redline scan is unreliable.

    Counting all redlines is unreliable here: deleting inside a pending
    tracked insertion splits its Insert redline without recording a deletion.
    """
    count = 0

    def _on_item(redline: Any) -> bool:
        nonlocal count
        if uno_read(lambda: redline.getPropertyValue("RedlineType"), None) == "Delete":
            count += 1
        return True

    reliable, _seen, _total = scan_redlines(doc, _on_item)
    return count if reliable else None


def _deletion_recorded(doc: Any, before: int, cells: list[Any]) -> bool:
    """True when a Delete redline was added or all visible text in *cells* became tracked-deleted.

    The second arm covers bands whose existing Delete redlines merge or shrink,
    leaving the Delete count level or lower even though the deletion was recorded.
    """
    after = _delete_redline_count(doc)
    if after is None:
        return False
    if after > before:
        return True
    return cells_all_tracked_deleted(cells, doc)


def _place_caret_in_cell(controller: Any, table: Any, name: str, cell_name: str) -> bool:
    """Place the view cursor at the end of *cell_name* and confirm it landed in *name*.*cell_name*."""
    # Use select(range) rather than gotoRange: gotoRange raises "no text selection" while a frame is selected.
    controller.select(table.getCellByName(cell_name).getEnd())
    # StopShellTimer flushes the pending shell switch so getViewCursor reflects the table cell.
    controller.getSelection()
    vc = controller.getViewCursor()
    if vc is None:
        return False
    vc_table = uno_read(lambda: vc.TextTable, None)
    vc_cell = uno_read(lambda: vc.Cell, None)
    if vc_table is None or vc_cell is None:
        return False
    return (
        uno_read(lambda: vc_table.getName(), None) == name
        and uno_read(lambda: vc_cell.CellName, None) == cell_name
    )


def _delete_writer_table_tracked(
    doc: Any,
    uno_ctx: Any,
    table: Any,
    name: str,
    *,
    pending_insertion: bool = False,
) -> None:
    """Delete a Writer table as a tracked change. Change tracking must already be on.

    Removing a table with removeTextContent (or removing every row) while change
    tracking is on produces no redline: the table vanishes unreviewable and any
    tracked changes pending inside it vanish with it. Placing the caret in a
    non-nested cell and running ``.uno:DeleteTable`` records the deletion; the
    table stays, struck through, until the change is accepted.

    The caret must land in a cell without a nested table (otherwise, when A1
    starts with a nested table, the caret lands inside the nested table and only
    that one is deleted), and its landing is confirmed via the view cursor:
    ``SwXTextView::select`` (``unotxvw.cxx``) ignores ``GotoTable`` failures
    (e.g. hidden sections), which would otherwise delete whichever table
    currently held the cursor.

    Empty rows get a U+200D anchor from Writer without a redline; clearing
    each anchor with tracking on gives every row its Delete redline so
    accepting removes the whole table, while Reject keeps the anchor as
    LibreOffice's own Delete Table does.

    When *pending_insertion* is true, LibreOffice takes the caller's own
    pending text back outright and leaves an empty shell that belongs to the
    same pending insertion; removing that shell finishes the deletion cleanly.

    Fails closed: if the caret cannot land, the redline scan is unreliable, or
    no deletion is recorded, this raises instead of reporting an unreviewable
    deletion as done.
    """
    controller = doc.getCurrentController()
    if controller is None:
        raise ToolExecutionError(
            "The document has no open view, so the deletion cannot be recorded and nothing was deleted. "
            "Ask the user to open the document in a window, then retry.",
            code="TABLE_DELETE_NOT_TRACKABLE",
        )
    cell_names = list(table.getCellNames() or ())
    cells = [table.getCellByName(n) for n in cell_names]
    empty_cells = [n for n, c in zip(cell_names, cells) if not c.getString()]
    target_cell = next(
        (n for n, c in zip(cell_names, cells) if not cell_hosted_table_names(c)),
        None,
    )
    if target_cell is None:
        raise ToolExecutionError(
            "Every cell of table '%s' hosts a nested table, so deleting it cannot be recorded; "
            "it was left in place. Ask the user to delete it (Table > Delete > Table)." % name,
            code="TABLE_DELETE_NOT_TRACKABLE",
        )
    before = _delete_redline_count(doc)
    if before is None:
        raise ToolExecutionError(
            "Could not scan tracked changes before deleting table '%s', so it was left in place. "
            "Retry once; if it fails again, ask the user to delete it." % name,
            code="TABLE_DELETE_NOT_TRACKABLE",
        )
    # Leave the user's cursor where it was around the table selection and dispatch.
    with KeepViewSelection(controller):
        if not _place_caret_in_cell(controller, table, name, target_cell):
            raise _cursor_refused(name)
        execute_dispatch(uno_ctx, controller, ".uno:DeleteTable")
        try:
            for cell_name in empty_cells:
                cell = table.getCellByName(cell_name)
                if cell.getString() == _EMPTY_ROW_ANCHOR:
                    cursor = cell.createTextCursor()
                    cursor.gotoStart(False)
                    cursor.goRight(1, True)
                    cursor.setString("")
        except Exception as exc:
            if is_disposal(exc):
                raise
            log.debug("table_delete: could not clear an empty-row anchor", exc_info=True)

    if _deletion_recorded(doc, before, cells):
        return
    if doc.getTextTables().hasByName(name):
        if pending_insertion:
            _remove_writer_table(doc, table, name, _not_nested())
            return
        raise ToolExecutionError(
            "LibreOffice did not record deleting table '%s' as a tracked change, so it was left in place. "
            % name
            + _NOT_RECORDED_HELP,
            code="TABLE_DELETE_NOT_TRACKABLE",
        )
    raise RuntimeError("the table was removed but no tracked change was recorded")


def _column_edges(table: Any) -> list[list[int]]:
    """Per-row horizontal cell edges (0 .. _UNO_TABLE_COLUMN_SUM) from TableColumnSeparators."""
    rows = table.getRows()
    n_rows = int(rows.getCount())
    edges: list[list[int]] = []
    for r_idx in range(n_rows):
        seps = rows.getByIndex(r_idx).getPropertyValue("TableColumnSeparators")
        edges.append([0, *(int(s.Position) for s in seps), _UNO_TABLE_COLUMN_SUM])
    return edges


def _column_grid(table: Any) -> list[list[_Box]]:
    """Per-row boxes ``(cell_name_or_None, left, right)`` from ``getCellNames()`` and separators."""
    names = _writer_named_cells(table)
    if any("." in name for name in names):
        # Untracked removeByIndex refuses too: unotbl.cxx:SwXTableColumns::removeByIndex / lcl_EnsureTableNotComplex.
        raise ValueError(
            "LibreOffice cannot delete a column from this table because it has split cells; "
            "empty that column's cells with table_set_cell instead (addresses from table_get_cells)."
        )
    by_pos = {
        pos: name
        for name in names
        if (pos := writer_cell_position(name)) is not None
    }
    return [
        [
            (by_pos.get((c_idx, r_idx)), row_edges[c_idx], row_edges[c_idx + 1])
            for c_idx in range(len(row_edges) - 1)
        ]
        for r_idx, row_edges in enumerate(_column_edges(table))
    ]


def _column_band(grid: list[list[_Box]], idx: int) -> list[tuple[int, int]]:
    """Grid coordinates ``(row, col)`` in column *idx*, mirroring ``swnewtable.cxx`` (``lcl_SearchSelBox`` / ``ExpandColumnSelection``)."""
    if not grid or idx < 0 or idx >= len(grid[0]):
        return []
    _name, lo, hi = grid[0][idx]
    mid = (lo + hi) // 2
    first, last = lo, hi
    picked: set[tuple[int, int]] = set()
    for r_idx, row in enumerate(grid):
        for c_idx, (_box_name, left, right) in enumerate(row):
            if right <= lo:
                continue
            hit = (
                (left >= lo or right >= mid or right - lo > lo - left)
                if right <= hi
                else (left <= mid or right - hi < hi - left)
            )
            if hit:
                picked.add((r_idx, c_idx))
                first = min(first, left)
                last = max(last, right)
            if right >= hi:
                break
    for r_idx, row in enumerate(grid):
        for c_idx, (_box_name, left, right) in enumerate(row):
            # 1-unit slack accounts for rounding of relative TableColumnSeparators positions.
            if left >= first - 1 and right <= last + 1:
                picked.add((r_idx, c_idx))
    return sorted(picked)


def _column_band_cells(table: Any, idx: int) -> list[str]:
    """Cell names in column *idx*."""
    grid = _column_grid(table)
    return [
        name
        for row, col in _column_band(grid, idx)
        if (name := grid[row][col][0]) is not None
    ]


def _band_cell_names(table: Any, axis: str, idx: int) -> list[str]:
    """Cell names in row or column *idx* of *table*."""
    if axis == "column":
        return _column_band_cells(table, idx)
    positions = {
        name: pos
        for name in _writer_named_cells(table)
        if (pos := writer_cell_position(name)) is not None
    }
    in_row = [(pos[0], name) for name, pos in positions.items() if pos[1] == idx]
    in_row.sort()
    return [name for _col, name in in_row]


def _row_spanning_cells(table: Any, idx: int) -> list[tuple[str, int]]:
    """Cells starting in row *idx* whose RowSpan exceeds 1, left to right, with that span."""
    spans = [
        (name, int(table.getCellByName(name).getPropertyValue("RowSpan")))
        for name in _band_cell_names(table, "row", idx)
    ]
    return [(name, span) for name, span in spans if span > 1]


_PORTION_LABELS = {
    "Annotation": "a comment",
    "Bookmark": "a bookmark",
    "Frame": "a frame, image or shape",
    "TextField": "a field",
    "Footnote": "a footnote",
}


def _cell_content(cell: Any) -> str | None:
    """Short label for what *cell* holds, or ``None`` when empty.

    Raw text counts, so a pending tracked deletion is content: removing it would lose the
    user's chance to reject it. AT_PARAGRAPH objects are not portions; only the paragraph's
    content enumeration lists them (``SwXParagraph::createContentEnumeration``,
    sw/source/core/unocore/unoparagraph.cxx). Nested tables are the caller's earlier check.
    """
    if cell.getString().strip("\n"):
        return _TEXT_CONTENT
    for para in iter_elements(cell.createEnumeration()):
        kind = non_text_portion_type(para, ignore_markers=True)
        if kind is not None:
            return _PORTION_LABELS.get(kind, kind)
        if enum_has_more(para.createContentEnumeration("com.sun.star.text.TextContent")):
            return "a frame, image or shape"
    return None


def _protected_cells(table: Any, names: list[str]) -> list[str]:
    """The cells of *table* among *names* that are marked ``IsProtected``."""
    return [name for name in names if table.getCellByName(name).getPropertyValue("IsProtected")]


def _protected_refusal(verb: str, what: str, cells: list[str]) -> str:
    """Refusal message when *what* ("this row", "table 'T'", "cell A1 of table 'T'") cannot be changed because *cells* are protected."""
    return (
        "Cannot %s %s: cell(s) %s are protected. Leave it and ask the user to unprotect "
        "them first (Table > Unprotect Cells), then %s it; do not change protected cells."
        % (verb, what, ", ".join(cells), verb)
    )


def _split_rows_call(row_span: int) -> str:
    """``table_split_cell`` call string for splitting a cell spanning *row_span* rows."""
    call = "table_split_cell (direction='rows', parts=%d)" % min(row_span, 20)
    if row_span > 20:
        return call + ", repeated while the delete is still refused"
    return call


def _row_refusal(cell_name: str, row_span: int, content: str) -> str:
    """Refusal message for deleting a row where a spanning non-empty cell starts."""
    prefix = (
        "Cannot delete this row: cell '%s' starts in it and spans %d rows, "
        "so its content (%s) would be deleted too. " % (cell_name, row_span, content)
    )
    call = _split_rows_call(row_span)
    if content == _TEXT_CONTENT:
        return prefix + (
            "Split it with %s, copy the text to keep into a lower part with table_set_cell "
            "(addresses from the returned cell_names), then delete the row. If table_get_cells "
            "shows the cell empty, its text is a pending tracked deletion: ask the user to accept "
            "or reject it first." % call
        )
    return prefix + (
        "table_set_cell cannot move that, so ask the user to move it out of the row. "
        "If the user agrees to lose it, split the cell with %s, then delete the row." % call
    )


def _column_span(grid: list[list[_Box]], left: int, right: int) -> int:
    """Number of table columns spanned by ``[left, right]`` across all rows' separators."""
    seps = sorted({right_edge for row in grid for _name, _left, right_edge in row[:-1]})
    count = 1
    last = left
    for p in seps:
        if last + 1 < p < right - 1:
            count += 1
            last = p
    return count


def _wide_box(grid: list[list[_Box]], idx: int, *, covered: bool = False) -> tuple[int, int, int, int] | None:
    """Widest merged box in column *idx*: ``(row, col, span, doomed_columns)``, or None.

    The delete guard counts named cells only; the split planner sets ``covered=True``
    because LibreOffice deletes covered boxes too.

    ``SwXTableColumns::removeByIndex`` (``sw/source/core/unocore/unotbl.cxx``) calls
    ``SwDoc::DeleteCol`` (``sw/source/core/docnode/ndtbl.cxx``), which expands the
    selection across any wider cell; a full-width row makes LibreOffice delete the whole table.
    """
    coords = [
        (row, col)
        for row, col in _column_band(grid, idx)
        if covered or grid[row][col][0] is not None
    ]
    rows = [row for row, _col in coords]
    if len(set(rows)) == len(rows):
        return None
    spans = [
        (_column_span(grid, grid[row][col][1], grid[row][col][2]), row, col)
        for row, col in coords
    ]
    span, row, col = max(spans, key=lambda item: item[0])
    left_min = min(grid[r][c][1] for r, c in coords)
    right_max = max(grid[r][c][2] for r, c in coords)
    doomed = _column_span(grid, left_min, right_max)
    return row, col, span, doomed


def _column_split_plan(grid: list[list[_Box]], idx: int) -> list[tuple[str, int]] | None:
    """Cells to split, with their part counts, so that column *idx* can be deleted, or None.

    Simulates equal splits (table_split_cell cannot set widths) and re-applies the delete guard.
    None means the equal parts would not line up with the other rows' edges, so a split would only
    fragment the table. Covered boxes count because LibreOffice deletes them too.
    """
    grid = [list(row) for row in grid]
    done: set[int] = set()
    steps: list[tuple[str, int]] = []
    while True:
        if idx >= len(grid[0]):
            spans = [_column_span(grid, left, right) for _name, left, right in grid[0]]
            row, col, parts = 0, spans.index(max(spans)), max(spans)
        else:
            wide = _wide_box(grid, idx, covered=True)
            if wide is None:
                return steps
            row, col, parts, _doomed = wide
        name, left, right = grid[row][col]
        # One split per row keeps the other rows' cell names valid, so the listed names stay correct.
        if name is None or row in done or not 2 <= parts <= 20:
            return None
        done.add(row)
        cuts = [left + (right - left) * k // parts for k in range(parts + 1)]
        grid[row][col:col + 1] = [(name, cuts[k], cuts[k + 1]) for k in range(parts)]
        # SwTable::SplitCol (sw/source/core/doc/tblrwcl.cxx:1157) expands across covered boxes below.
        for r2 in range(row + 1, len(grid)):
            match = next(
                (c2 for c2, (n2, l2, r2_edge) in enumerate(grid[r2]) if n2 is None and l2 == left and r2_edge == right),
                None,
            )
            if match is None:
                break
            grid[r2][match:match + 1] = [(None, cuts[k], cuts[k + 1]) for k in range(parts)]
            done.add(r2)
        steps.append((name, parts))


def _column_refusal(table: Any, idx: int) -> str | None:
    """Refusal message when deleting column *idx* is blocked by merged/banner cells, or None."""
    grid = _column_grid(table)
    if idx >= max(len(row) for row in grid):
        return None  # check_table_band reports the plain out-of-range index
    if idx >= len(grid[0]):
        reason = (
            "Cannot delete column %d: a Writer column index counts the cells of the first row, "
            "and the first row has only %d." % (idx, len(grid[0]))
        )
    else:
        wide = _wide_box(grid, idx)
        if wide is None:
            return None
        row, col, span, doomed = wide
        reason = (
            "Cannot delete this column: cell '%s' spans %d columns, so %d columns "
            "would be deleted." % (grid[row][col][0], span, doomed)
        )
    steps = _column_split_plan(grid, idx)
    if steps is None:
        return reason + (
            " Do not split cells: the parts would not line up with the other rows. "
            "Empty that column's cells (not protected ones) with table_set_cell instead "
            "(addresses from table_get_cells), or ask the user to delete the column."
        )
    splits = ", ".join("'%s' (parts=%d)" % step for step in steps)
    return reason + (
        " Split %s with table_split_cell (direction='columns'), move the text to keep into "
        "another part with table_set_cell, then delete the column." % splits
    )


def _pending_insertions(doc: Any, names: list[str]) -> set[str]:
    """Return leading *names* whose own creation is a pending Insert redline.

    ``SwXRedline::getPropertyValue`` (``sw/source/core/unocore/unoredline.cxx``)
    exposes ``RedlineStart`` as the table object (``SwNodeType::Table``) only
    when the table itself was inserted; typed text inside an existing cell
    starts in that cell instead.
    """
    if not names:
        return set()
    pos = {name: idx for idx, name in enumerate(names)}
    pending: set[str] = set()

    def _on_item(redline: Any) -> bool:
        if uno_read(lambda: redline.getPropertyValue("RedlineType"), None) != "Insert":
            return True
        start = uno_read(lambda: redline.getPropertyValue("RedlineStart"), None)
        if start is None or not uno_read(
            lambda: supports_service(start, "com.sun.star.text.TextTable"), False
        ):
            return True
        start_name = uno_read(start.getName, None)
        if start_name not in pos:
            return True
        i = pos[start_name]
        j = len(names)
        end = uno_read(lambda: redline.getPropertyValue("RedlineEnd"), None)
        if end is not None and uno_read(
            lambda: supports_service(end, "com.sun.star.text.TextTable"), False
        ):
            end_name = uno_read(end.getName, None)
            if end_name in pos:
                j = pos[end_name]
        pending.update(names[i:j])
        return True

    scan_redlines(doc, _on_item)
    return pending


def delete_leading_tables(session: Any, doc: Any, uno_ctx: Any) -> None:
    """Delete leading body TextTables before a full-document replace.

    Body ``gotoStart`` (``sw/source/core/unocore/unoobj.cxx:SwXTextCursor::gotoStart``)
    steps past leading tables, so ``replace_full_document`` would leave them behind.
    A table inside a section stops the walk, because ``replace_full_document``
    deletes that section whole.
    """
    tables = list(
        itertools.takewhile(
            lambda el: supports_service(el, "com.sun.star.text.TextTable") and el.getPropertyValue("TextSection") is None,
            iter_elements(doc.getText().createEnumeration()),
        )
    )
    if not tables:
        return
    if not uno_read(lambda: is_recording_changes(doc), False):
        # Reverse order: a deleted table passes its page style to the paragraph
        # after it, not to a following table.
        for table in reversed(tables):
            _remove_writer_table(doc, table, table.getName(), _not_nested())
        return
    # Compute pending insertions once before any removal, because deleting one
    # table can shift the redline start off the next table node.
    names = [table.getName() for table in tables]
    pending = _pending_insertions(doc, names)
    for table, name in zip(tables, names):
        # One change per table: a redline end inside a cell and one in the body
        # cannot share a review anchor.
        session.record_mutation(
            lambda t=table, n=name: _delete_writer_table_tracked(
                doc, uno_ctx, t, n, pending_insertion=n in pending
            )
        )


def _delete_band_tracked(doc: Any, uno_ctx: Any, table: Any, name: str, axis: str, idx: int) -> None:
    """Delete one Writer row or column as a tracked change via ``.uno:DeleteRows`` / ``.uno:DeleteColumns``."""
    band = table.getRows() if axis == "row" else table.getColumns()
    check_table_band(band, "delete", idx, axis)
    band_names = _band_cell_names(table, axis, idx)
    if not band_names:
        raise ValueError("Cannot locate %s %d in table '%s'." % (axis, idx, name))
    if axis == "row":
        target_cell = band_names[0]
        # SwFEShell::DeleteRow (sw/source/core/frmedt/fetab.cxx) selects from the row's first box
        # to its last, and SwTable::CreateSelection (sw/source/core/table/swnewtable.cxx) extends
        # that selection down by either box's row span, so the rows below would be struck too.
        edge = next(
            (s for s in _row_spanning_cells(table, idx) if s[0] in (band_names[0], band_names[-1])), None
        )
        if edge is not None:
            cell_name, row_span = edge
            raise ToolExecutionError(
                "Cannot delete this row as a tracked change: cell '%s', first or last in the row, spans "
                "%d rows, so LibreOffice would mark the rows below for deletion too. Split it with %s, "
                "then delete the row."
                % (cell_name, row_span, _split_rows_call(row_span)),
                code="ROW_HAS_SPANNING_CELL",
            )
    else:
        col_target = next((n for n in band_names if writer_cell_position(n) == (idx, 0)), None)
        if col_target is None:
            raise ValueError("Cannot locate %s %d in table '%s'." % (axis, idx, name))
        target_cell = col_target

    controller = doc.getCurrentController()
    if controller is None:
        raise ToolExecutionError(
            "The document has no open view, so the deletion cannot be recorded and nothing was deleted. "
            "Ask the user to open the document in a window, then retry.",
            code="TABLE_DELETE_NOT_TRACKABLE",
        )

    band_cells = [table.getCellByName(n) for n in band_names]
    before = _delete_redline_count(doc)
    if before is None:
        raise ToolExecutionError(
            "Could not scan tracked changes before deleting this %s of table '%s', so it was left in place. "
            "Retry once; if it fails again, ask the user to delete it." % (axis, name),
            code="TABLE_DELETE_NOT_TRACKABLE",
        )
    cmd = ".uno:DeleteRows" if axis == "row" else ".uno:DeleteColumns"
    with KeepViewSelection(controller):
        if not _place_caret_in_cell(controller, table, name, target_cell):
            raise _cursor_refused(name)
        # Dispatch State cannot confirm success (it is 1 on an already-marked row and 0 on a working .uno:DeleteTable).
        execute_dispatch(uno_ctx, controller, cmd)

    if not _deletion_recorded(doc, before, band_cells):
        raise ToolExecutionError(
            "LibreOffice did not record deleting this %s as a tracked change, so it was left in place. "
            % axis
            + _NOT_RECORDED_HELP,
            code="TABLE_DELETE_NOT_TRACKABLE",
        )


def _mutate_or_rollback(doc: Any, uno_ctx: Any, title: str, apply: Callable[[], Any]) -> Any:
    """Run a Writer table mutation with optional review inside an undo rollback guard."""
    # The guard stays outside record_mutation, which keeps its own undo group open while apply
    # runs: XUndoManager.undo refuses inside an open context (impl_doUndoRedo in
    # framework/source/fwe/helper/undomanagerhelper.cxx).
    with UndoStepOrRollback(doc, title):
        return run_writer_mutation_with_optional_review(doc, uno_ctx, apply)


def _protected_with_hosts(doc: Any, table: Any, names: list[str], nesting: dict[str, Any]) -> list[str]:
    """Protected cells among *names*, plus protected host cells up the nesting chain.

    The UI refuses edits inside a protected host cell (``SwFrame::IsProtected``, ``trvlfrm.cxx``,
    which walks up through the host cell frames), whereas ``IsProtected`` is not inherited on UNO
    cells, so the hosts are read one by one.
    """
    cells = _protected_cells(table, names)
    while nesting["is_nested"]:
        host_name, host_cell = nesting["parent_table"], nesting["parent_cell"]
        host = _get_table(doc, host_name)
        cells += ["%s of table '%s'" % (n, host_name) for n in _protected_cells(host, [host_cell])]
        nesting = _nesting_for(doc, host, host_name)
    return cells


def _require_no_protected_cells(doc: Any, table: Any, name: str, nesting: dict[str, Any]) -> None:
    """Refuse to delete a table that has, or sits inside, a protected cell.

    LibreOffice's Delete Table does nothing then (SwDoc::DeleteRowCol, ndtbl.cxx; SwEditShell::Delete,
    eddel.cxx), but SwXTextTable::dispose (unotbl.cxx) passes RowColMode::DeleteProtected, so
    removeTextContent would delete it. Like LibreOffice, cells of tables nested inside do not count.
    """
    cells = _protected_with_hosts(doc, table, _writer_named_cells(table), nesting)
    if cells:
        raise ToolExecutionError(_protected_refusal("delete", "table '%s'" % name, cells), code="CELLS_PROTECTED")


def delete_writer_table(doc: Any, uno_ctx: Any, table: Any, name: str, nesting: dict[str, Any]) -> bool:
    """Remove *table*. True when the deletion was recorded as a tracked change.

    Refuses with ``CELLS_PROTECTED`` when the table has, or sits inside, a protected cell.
    ``table_delete`` and ``apply_content`` both call this. The caller
    wraps it in the EditReviewSession it already has, so RecordChanges is on
    before the tracked path runs. A second review wrapper here would nest
    sessions and tag the redlines twice.
    """
    _require_no_protected_cells(doc, table, name, nesting)
    if uno_read(lambda: is_recording_changes(doc), False):
        _delete_writer_table_tracked(doc, uno_ctx, table, name)
        return True
    _remove_writer_table(doc, table, name, nesting)
    return False


def record_table_deletions(session: Any, doc: Any, ctx: Any, doomed: list[tuple[Any, str]]) -> bool:
    """Delete tables inside the caller's already-open review session.

    Returns True when at least one deletion was a tracked change. Does not
    open a second EditReviewSession: apply_content is already in
    one, and nesting would tag the same redlines twice.
    """
    tracked = False
    uno_ctx = resolve_uno_context(ctx)
    planned = [(table, name, _nesting_for(doc, table, name)) for table, name in doomed]
    for table, name, nesting in planned:

        def _apply(table: Any = table, name: str = name, nesting: dict[str, Any] = nesting) -> None:
            nonlocal tracked
            if delete_writer_table(doc, uno_ctx, table, name, nesting):
                tracked = True

        session.record_mutation(_apply)
    return tracked


def _remove_writer_table(doc: Any, table: Any, name: str, nesting: dict[str, Any]) -> None:
    """Remove a TextTable from the XText that contains it (body or host cell).

    Prefer ``getAnchor()`` (same pattern as bookmarks). If that is unusable,
    fall back to nesting: host cell for nested tables, ``doc.getText()`` for
    top-level. Deleting a host table also destroys nested children — that is
    intentional (``table_delete``), unlike the row/column refuse-guards.
    """
    try:
        table.getAnchor().getText().removeTextContent(table)
        return
    except Exception as exc:
        if is_disposal(exc):
            raise
        log.debug("table.getAnchor() remove failed for '%s'; using nesting fallback", name, exc_info=True)
    if nesting.get("is_nested"):
        parent = _get_table(doc, str(nesting.get("parent_table") or ""))
        host = parent.getCellByName(str(nesting.get("parent_cell") or ""))
        host.removeTextContent(table)
        return
    doc.getText().removeTextContent(table)


def _hosted_in_band(table: Any, axis_arg: str, idx: int) -> list[str]:
    """Nested table names hosted in the row/column about to be deleted.

    Cell names renumber per row after a merge, so a column is found from row
    geometry rather than column index alone.
    """
    hosted: list[str] = []
    for cell_name in _band_cell_names(table, axis_arg, idx):
        def _read_cell(c: str = cell_name) -> Any:
            return table.getCellByName(c)

        cell = uno_read(_read_cell, FAILED)
        if cell is not FAILED:
            hosted.extend(cell_hosted_table_names(cell))
    return list(dict.fromkeys(hosted))


class TableList(ToolWriterTableBase):
    name: str | None = "table_list"
    description: str = (
        "List tables with name, rows, cols, and cell_count. "
        "Writer: if cell_count is not rows times cols the table is not a rectangle — use "
        "table_get_cells cells. "
        "Each row includes nesting (direct parent table/cell) and nested_in_cells (host cells that "
        "contain nested tables — table_set_cell keeps those tables; do not delete the host row/column). "
        "Draw/Impress: also page and shape index; cell_count is rows times cols."
    )
    is_mutation: bool | None = False
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        if is_draw(ctx.doc):
            # Lazy: cross-app; Draw table helpers load only when the target is a Draw/Impress document.
            from plugin.draw.tables import list_draw_tables

            out = list_draw_tables(ctx.doc)
            return {"status": "ok", "count": len(out), "tables": out}
        tables = _tables(ctx.doc)
        nesting_by_name, hosted, cell_counts = _writer_nesting(ctx.doc)
        out = []
        for name in tables.getElementNames():
            try:
                table_obj = tables.getByName(name)
                rows, cols = table_dims(table_obj)
            except Exception as exc:
                # One bad or corrupt table must not fail the whole table_list call:
                # re-raise document disposal, log a warning, and skip the table.
                if is_disposal(exc):
                    raise
                log.warning("table_list: skipping unreadable table '%s': %s", name, exc)
                continue
            cell_count = cell_counts.get(name)
            if cell_count is None:
                cell_count = rows * cols
            out.append({
                "name": name,
                "rows": rows,
                "cols": cols,
                "cell_count": cell_count,
                "nesting": nesting_by_name.get(name, _not_nested()),
                "nested_in_cells": hosted.get(name, {}),
            })
        return {"status": "ok", "count": len(out), "tables": out}


def _cell_text(deletions: TrackedDeletions, cell: Any, hosted: list[str]) -> str:
    """Visible plain text of *cell*.

    Host cells omit concatenated inner-table text. ``get_string_without_tracked_deletions``
    hides pending tracked deletions: ``getString()`` also returns deleted text, so a pending
    "velha" -> "nova" would otherwise read "celula novavelha".
    """
    state = deletions.state_at(cell)
    return cell_plain_siblings(cell, state) if hosted else get_string_without_tracked_deletions(cell, state)


class TableGetCells(ToolWriterTableBase):
    name: str | None = "table_get_cells"
    description: str = (
        "Return Writer cell text as cells (name map) and Draw/Impress as matrix (row-major by position). "
        "Empty string is an empty cell; a missing name is not a cell. "
        "A failed Draw/Impress cell read is an error, not an empty string. "
        "Writer: rows and cols can be smaller than the real grid when cells are merged — follow cells. "
        "Draw/Impress: read matrix; when cell is set, matrix is that one cell only. "
        "Optional cell reads one address (Writer via getCellNames, Draw via A1). "
        "Writer host cells that contain nested tables return only the host cell's own paragraphs; "
        "nested_in_cells names those cells. "
        "table_set_cell on a host cell updates that host text and leaves nested tables."
    )
    is_mutation: bool | None = False
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Table name from table_list."},
            "cell": {
                "type": "string",
                "description": "Optional. One A1-style address; return that cell only.",
            },
            "page": _PAGE_PROPERTY,
            "index": {"type": "integer", "description": "Draw/Impress: shape index on the page."},
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = str(kwargs.get("name") or "").strip()
        cell_raw = str(kwargs.get("cell") or "").strip()
        try:
            if is_draw(ctx.doc):
                # Lazy: cross-app; Draw table helpers load only when the target is a Draw/Impress document.
                from plugin.draw.tables import get_draw_cell, get_draw_cells, resolve_draw_table

                entry = resolve_draw_table(ctx.doc, name=name, page=kwargs.get("page"), index=kwargs.get("index"))
                payload = _draw_ok(entry, name, rows=entry.get("rows"), cols=entry.get("cols"))
                if cell_raw:
                    text = get_draw_cell(entry, cell_raw)
                    payload["cell"] = cell_raw
                    payload["matrix"] = [[text]]
                else:
                    payload["matrix"] = get_draw_cells(entry)
                return payload
            table = _require_table(ctx.doc, name)
            rows, cols = table_dims(table)
            all_cell_names = _writer_named_cells(table)
            resolved = ""
            if cell_raw:
                match = _resolve_cell_name(cell_raw, all_cell_names)
                if match is None:
                    return self._tool_error(_unknown_cell_message(cell_raw, name, all_cell_names))
                resolved = match
                cell_names = [resolved]
            else:
                cell_names = all_cell_names

            deletions = TrackedDeletions(ctx.doc)
            cells: dict[str, str] = {}
            nested_in_cells: dict[str, list[str]] = {}
            for cell_name in cell_names:
                try:
                    c = table.getCellByName(cell_name)
                    hosted = cell_hosted_table_names(c)
                    if hosted:
                        nested_in_cells[cell_name] = hosted
                    cells[cell_name] = _cell_text(deletions, c, hosted)
                except Exception as exc:
                    # Per-cell read errors must not disappear into a bare continue.
                    # That also swallows document disposal. Re-raise disposal and log
                    # the cell read failure.
                    if is_disposal(exc):
                        raise
                    log.warning("table_get_cells: could not read cell '%s' in table '%s': %s", cell_name, name, exc)
                    continue
            if resolved and resolved not in cells:
                return self._tool_error(
                    "Could not read cell '%s' in table '%s'." % (cell_raw, name)
                )
            payload = {
                "status": "ok",
                "table_name": name,
                "rows": rows,
                "cols": cols,
                "cell_names": list(cells.keys()),
                "cells": cells,
                "nesting": _nesting_for(ctx.doc, table, name),
                "nested_in_cells": nested_in_cells,
            }
            if resolved:
                payload["cell"] = resolved
            return payload
        except ValueError as ve:
            return self._tool_error(str(ve))


class TableSetCell(ToolWriterTableBase):
    name: str | None = "table_set_cell"
    description: str = (
        "Set the plain-text content of ONE table cell, addressed A1-style (e.g. 'B2'). "
        "Writer: the new text keeps the character formatting of the text it replaces, writing the "
        "cell's current text changes nothing, and the write is recorded as a tracked change while "
        "Track Changes or review mode is on. A cell holding a formula or a number value becomes plain "
        "text (the formula is removed). A cell that hosts a nested table keeps that table and "
        "rewrites only the host paragraphs; edit the nested table by its own name. Refuses a "
        "protected cell (CELLS_PROTECTED) and, while Track Changes is off, a cell holding a pending "
        "tracked deletion (MATCH_CROSSES_DELETION)."
    )
    is_mutation: bool | None = True
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Table name from table_list."},
            "cell": {"type": "string", "description": "A1-style cell address, e.g. 'B2'."},
            "text": {"type": "string", "description": "New plain text for the cell."},
            "page": _PAGE_PROPERTY,
            "index": {"type": "integer", "description": "Draw/Impress: shape index on the page."},
        },
        "required": ["cell", "text"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = str(kwargs.get("name") or "").strip()
        cell_raw = kwargs["cell"].strip()
        # type/required checks are done by the tool schema in ToolRegistry
        text = kwargs["text"]
        try:
            if is_draw(ctx.doc):
                # Lazy: cross-app; Draw table helpers load only when the target is a Draw/Impress document.
                from plugin.draw.tables import resolve_draw_table, set_draw_cell

                entry = resolve_draw_table(ctx.doc, name=name, page=kwargs.get("page"), index=kwargs.get("index"))
                old, new = set_draw_cell(entry, cell_raw, str(text))
                return _draw_ok(
                    entry,
                    name,
                    cell=cell_raw,
                    old_text=old,
                    new_text=new,
                )
            table = _require_table(ctx.doc, name)
            names = _writer_named_cells(table)
            cell_name = _resolve_cell_name(cell_raw, names)
            if cell_name is None:
                return self._tool_error(
                    _unknown_cell_message(cell_raw, name, names)
                )
            cell = table.getCellByName(cell_name)
            protected = _protected_with_hosts(ctx.doc, table, [cell_name], _nesting_for(ctx.doc, table, name))
            if protected:
                return self._tool_error(
                    _protected_refusal("change", "cell %s of table '%s'" % (cell_name, name), protected),
                    code="CELLS_PROTECTED",
                )
            nested = cell_hosted_table_names(cell)
            deletions = TrackedDeletions(ctx.doc)
            new = str(text)
            result: dict[str, Any] = {
                "status": "ok",
                "table_name": name,
                "cell": cell_name,
                "old_text": _cell_text(deletions, cell, nested),
                "new_text": new,
            }
            if nested:
                result["nested_tables"] = nested
            if result["old_text"] == new:
                return result
            uno_ctx = resolve_uno_context(ctx)
            recorded = False

            def _apply() -> None:
                nonlocal recorded
                if nested:
                    # setString on a host cell wipes nested TextTables. Rewrite host paragraphs instead.
                    set_host_paragraphs(cell, new)
                    # The review session has already turned recording on in Record/Wait, and a user
                    # who records has it on already.
                    recorded = is_recording_changes(ctx.doc)
                    return
                span = cell.createTextCursor()
                span.gotoStart(False)
                span.gotoEnd(True)
                # An untracked replace writes into the struck run, and setString removes another
                # author's deletion.
                if not is_recording_changes(ctx.doc) and deletions.overlaps(span):
                    raise ToolExecutionError(
                        _CELL_DELETION_REFUSAL % (cell_name, name),
                        code="MATCH_CROSSES_DELETION",
                    )
                # SwXCell::setString (sw_setString, unotbl.cxx) clears the formula and value. An
                # in-place replace would keep them, and Writer would recompute the cell text over
                # the edit.
                if cell.getType().value in ("VALUE", "FORMULA"):
                    cell.setString(new)
                    recorded = is_recording_changes(ctx.doc)
                else:
                    wrote = replace_preserving_format(
                        ctx.doc, span, new, uno_ctx, in_undo_context=True, split_author=False
                    )
                    recorded = wrote and is_recording_changes(ctx.doc)

            _mutate_or_rollback(ctx.doc, uno_ctx, _("WriterAgent: Set table cell"), _apply)
            if recorded:
                flag_tracked(result)
                return annotate_review_status(result)
            return result
        except ValueError as ve:
            return self._tool_error(str(ve))


class ManageTableStructure(ToolWriterTableBase):
    """Insert or delete one row/column. The four former skinny tools shared table_name + index."""

    name: str | None = "table_manage_structure"
    description: str = (
        "Insert or delete one table row or column. "
        "index is 0-based (for insert, equal to the current count appends at the end). "
        "Cannot delete the last remaining row or column. "
        "A Writer column index counts the cells of the first row. An insert shifts later indexes at once, "
        "also while changes are recorded; a tracked delete shifts nothing until the user accepts it. "
        "Writer: refuses delete if the row/column hosts a nested table (that would destroy it); "
        "refuses to delete a row or column that holds a protected cell; "
        "refuses to delete a row in which a non-empty cell spanning several rows starts (as a tracked change, also an empty one that is first or last in the row); "
        "refuses to delete a column that a wider (merged) cell covers together with other columns; "
        "when Track Changes or review mode is on, delete is recorded as a tracked change."
    )
    is_mutation: bool | None = True
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "action": enum_param(["insert", "delete"], "insert or delete one band member."),
            "axis": enum_param(["row", "column"], "Whether to edit rows or columns."),
            "name": {"type": "string", "description": "Table name from table_list."},
            "index": {
                "type": "integer",
                "description": "0-based row or column index (insert at count = append).",
            },
            "page": _PAGE_PROPERTY,
            "shape_index": {
                "type": "integer",
                "description": "Draw/Impress: table shape index (not the row/column index).",
            },
        },
        "required": ["action", "axis", "index"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        action = kwargs["action"]
        axis_arg = kwargs["axis"]
        assert action in ("insert", "delete")
        assert axis_arg in ("row", "column")
        name = str(kwargs.get("name") or "").strip()
        # type/required checks are done by the tool schema in ToolRegistry
        idx = kwargs["index"]
        assert isinstance(idx, int) and not isinstance(idx, bool)
        if idx < 0:
            return self._tool_error("index must be non-negative.")
        insert = action == "insert"
        try:
            if is_draw(ctx.doc):
                # Lazy: cross-app; Draw table helpers load only when the target is a Draw/Impress document.
                from plugin.draw.tables import manage_draw_structure, resolve_draw_table

                entry = resolve_draw_table(
                    ctx.doc,
                    name=name,
                    page=kwargs.get("page"),
                    index=kwargs.get("shape_index"),
                )
                rows, cols = manage_draw_structure(entry, action, axis_arg, idx)
                return _draw_ok(entry, name, rows=rows, cols=cols)
            table = _require_table(ctx.doc, name)
            band = table.getRows() if axis_arg == "row" else table.getColumns()
            count = band.getCount()
            if not insert and idx < count and count > 1:
                nested = _hosted_in_band(table, axis_arg, idx)
                if nested:
                    return self._tool_error(
                        "Cannot delete this %s: it contains nested table(s) %s. If the user wants them gone too, "
                        "delete them first with table_delete (name=...), then delete the %s; a nested table whose "
                        "deletion is recorded as a tracked change stays until the user accepts it. Otherwise leave "
                        "the %s and edit its other cells with table_set_cell."
                        % (axis_arg, ", ".join(nested), axis_arg, axis_arg)
                    )
                # SwDoc::DeleteRow / DeleteCol (ndtbl.cxx) skip protected cells silently.
                protected = _protected_cells(table, _band_cell_names(table, axis_arg, idx))
                if protected:
                    return self._tool_error(
                        _protected_refusal("delete", "this %s" % axis_arg, protected),
                        code="CELLS_PROTECTED",
                    )
                if axis_arg == "row":
                    # PrepareDelBoxes (sw/source/core/table/swnewtable.cxx) hands the covered box below only
                    # the remaining span, so whatever the spanning cell holds goes with its start row.
                    for cell_name, row_span in _row_spanning_cells(table, idx):
                        found = _cell_content(table.getCellByName(cell_name))
                        if found is not None:
                            return self._tool_error(
                                _row_refusal(cell_name, row_span, found),
                                code="ROW_HAS_SPANNING_CELL",
                            )
            # Outside the idx < count and count > 1 block because count is row 0's box count (SwXTableColumns::getCount).
            if not insert and axis_arg == "column":
                refusal = _column_refusal(table, idx)
                if refusal is not None:
                    return self._tool_error(refusal, code="COLUMN_HAS_SPANNING_CELL")
            if insert:
                edit_table_band(band, action, idx, axis_arg)
                tracked = False
            else:
                uno_ctx = resolve_uno_context(ctx)

                def _apply() -> bool:
                    if uno_read(lambda: is_recording_changes(ctx.doc), False):
                        _delete_band_tracked(ctx.doc, uno_ctx, table, name, axis_arg, idx)
                        return True
                    edit_table_band(band, action, idx, axis_arg)
                    # removeByIndex ignores the outcome of SwDoc::DeleteRow/DeleteCol, and a
                    # protected cell merged from above is not in the band's names but may still block.
                    if band.getCount() == count:
                        raise ToolExecutionError(
                            "LibreOffice left this %s in place. A protected merged cell may reach into it "
                            "(the user can clear that with Table > Unprotect Cells), or the table may be a DDE link. "
                            "Leave it and tell the user; other rows and columns can still be deleted."
                            % axis_arg,
                            code="TABLE_BAND_NOT_DELETED",
                        )
                    return False

                tracked = bool(_mutate_or_rollback(ctx.doc, uno_ctx, "WriterAgent: Delete %s" % axis_arg, _apply))
            rows, cols = table_dims(table)
            result: dict[str, Any] = {"status": "ok", "table_name": name, "rows": rows, "cols": cols}
            if tracked:
                label = "Row" if axis_arg == "row" else "Column"
                flag_tracked(result)
                result["message"] = (
                    "%s marked for deletion as a tracked change: it stays in the table, "
                    "so later indexes do not shift, until the user accepts. Do not accept or reject it yourself."
                    % label
                )
            return result
        except ValueError as ve:
            return self._tool_error(str(ve))


class TableMergeCells(ToolWriterTableBase):
    """Merge a rectangular block of Writer table cells into one cell."""

    name: str | None = "table_merge_cells"
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True
    description: str = (
        "Merge a rectangular block of Writer table cells into one cell. Each merged cell's text "
        "becomes its own paragraph, empty cells add nothing, and nested tables are kept. If the "
        "block cuts through an already-merged cell, LibreOffice grows the block to cover it. Cell "
        "names are renumbered per row afterwards (after merging A1:B2 in a 3×3 table the old C1 "
        "is B1), so use the returned cell_names or table_get_cells before the next edit. Ctrl+Z "
        "undoes it in one step. Not a tracked change even when review mode is on."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Table name from table_list."},
            "cell_range": {
                "type": "string",
                "description": "Two corner addresses joined by ':', e.g. 'A1:B2' (either order).",
            },
        },
        "required": ["name", "cell_range"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["name"].strip()
        corners = [part.strip() for part in kwargs["cell_range"].split(":")]
        if len(corners) != 2 or not all(corners):
            return self._tool_error(
                "cell_range must be two cell addresses joined by ':', e.g. 'A1:B2'.",
                code="INVALID_PARAM",
            )
        try:
            table, (first, last) = _resolve_writer_cells(ctx.doc, name, corners)
        except ValueError as ve:
            return self._tool_error(str(ve))
        if first == last:
            return self._tool_error("cell_range must cover at least two cells.", code="INVALID_PARAM")
        # createCursorByCellName raises a bare RuntimeException on a name that is not a real cell
        # (e.g. lower-case 'a1'), so both corners are resolved against getCellNames() first.
        cursor = table.createCursorByCellName(first)
        # SwDoc::MergeTable sets RedlineFlags::Ignore (sw/source/core/docnode/ndtbl.cxx), so merges
        # are never recorded as tracked changes; this is not wrapped in run_writer_mutation_with_optional_review.
        # mergeRange returns False on a DDE table or an old-model table whose block LibreOffice rejects.
        if not (cursor.gotoCellByName(last, True) and cursor.mergeRange()):
            return self._tool_error(
                "LibreOffice could not merge %s:%s in table '%s'. "
                "Leave the cells as they are and edit their text with table_set_cell."
                % (first, last, name)
            )
        # After mergeRange the cursor sits on the merged box, so getRangeName() is its name.
        return _writer_grid_ok(table, name, merged_cell=str(cursor.getRangeName()))


class TableSplitCell(ToolWriterTableBase):
    """Split one Writer table cell into 2-20 parts as rows or columns."""

    name: str | None = "table_split_cell"
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True
    description: str = (
        "Split one Writer table cell into 2-20 parts, as rows or as columns. The cell's text and "
        "any nested table stay in the first part; the new parts are empty. Splitting a merged cell "
        "into as many parts as it spans undoes the merge. Cell names are renumbered afterwards, so "
        "use the returned cell_names. Ctrl+Z undoes it in one step. Not a tracked change even when "
        "review mode is on."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Table name from table_list."},
            "cell": {"type": "string", "description": "A1-style cell address from cell_names."},
            "direction": enum_param(
                ["rows", "columns"],
                "rows stacks the parts top to bottom; columns puts them side by side.",
            ),
            "parts": {
                "type": "integer",
                "minimum": 2,
                "maximum": 20,
                "description": "Number of cells the cell becomes (2-20).",
            },
        },
        "required": ["name", "cell", "direction", "parts"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["name"].strip()
        cell_raw = kwargs["cell"].strip()
        direction = kwargs["direction"]
        parts = kwargs["parts"]
        # Upper bound matches the Split Cells dialog spin button (cui/uiconfig/ui/splitcellsdialog.ui,
        # adjustment upper 20). The UNO path skips the dialog's minimum-width check.
        if not 2 <= parts <= 20:
            return self._tool_error("parts must be an integer from 2 to 20.", code="INVALID_PARAM")
        try:
            table, (cell_name,) = _resolve_writer_cells(ctx.doc, name, [cell_raw])
        except ValueError as ve:
            return self._tool_error(str(ve))
        cursor = table.createCursorByCellName(cell_name)
        # Count is the number of new cells, and Horizontal=True adds horizontal dividing lines
        # (more rows): SwXTextTableCursor::splitRange passes !Horizontal as bVert to SwDoc::SplitTable.
        # SwDoc::SplitTable records no redline, so it is not wrapped in run_writer_mutation_with_optional_review.
        if not cursor.splitRange(parts - 1, direction == "rows"):
            return self._tool_error(
                "LibreOffice could not split cell %s in table '%s'. "
                "Leave the cells as they are and edit their text with table_set_cell."
                % (cell_name, name)
            )
        return _writer_grid_ok(table, name, cell=cell_name, direction=direction, parts=parts)


class TableInsert(ToolWriterTableBase):
    name: str | None = "table_insert"
    description: str = (
        "Insert a table. Writer: text table at the view cursor (or document end); "
        "pass parent + cell to nest inside an existing table cell (inserts at the cell end). "
        "Draw/Impress: TableShape. parent/cell are Writer-only. "
        "Optional data is a 2D array of cell strings."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "rows": {"type": "integer", "description": "Number of rows"},
            "columns": {"type": "integer", "description": "Number of columns"},
            "data": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
                "description": "2D cell strings",
            },
            "parent": {
                "type": "string",
                "description": "Writer: host table name from table_list (requires cell).",
            },
            "cell": {
                "type": "string",
                "description": "Writer: A1-style host cell (requires parent).",
            },
            "page": page_index_param("Draw/Impress only; the active page if omitted."),
            "x": {"type": "integer", "description": "Draw/Impress: X position, 1/100 mm (default: 3000)"},
            "y": {"type": "integer", "description": "Draw/Impress: Y position, 1/100 mm (default: 4000)"},
            "width": {"type": "integer", "description": "Draw/Impress: width, 1/100 mm (default: 20000)"},
            "height": {"type": "integer", "description": "Draw/Impress: height, 1/100 mm (default: 10000)"},
        },
        "required": ["rows", "columns"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        parent = str(kwargs.get("parent") or "").strip()
        cell_raw = str(kwargs.get("cell") or "").strip()
        try:
            # The Draw branch sits inside the try. Outside it,
            # insert_draw_table raises past the tool error. The Writer path
            # already catches; this matches it.
            if is_draw(ctx.doc):
                # Lazy: cross-app; Draw table helpers load only when the target is a Draw/Impress document.
                from plugin.draw.tables import insert_draw_table

                if parent or cell_raw:
                    return self._tool_error("parent and cell are Writer-only (Draw has no nested text tables).")
                try:
                    result = insert_draw_table(ctx, **kwargs)
                except ToolExecutionError as exc:
                    return format_error_payload(exc)
                return result

            if parent and not cell_raw:
                return self._tool_error("cell is required when parent is set.")
            if cell_raw and not parent:
                return self._tool_error("parent is required when cell is set.")
            # type/required checks are done by the tool schema in ToolRegistry
            rows = kwargs["rows"]
            columns = kwargs["columns"]
            if rows < 1 or columns < 1:
                return self._tool_error("rows and columns must be at least 1.")

            data = kwargs.get("data")
            assert data is None or isinstance(data, (list, tuple)), type(data)
            if data:
                check_grid(data, rows, columns, reject_non_list=False)

            doc = ctx.doc
            host_cell_name = ""
            written_cells = 0
            created_name = ""

            def _apply() -> None:
                nonlocal host_cell_name, written_cells, created_name
                table = doc.createInstance("com.sun.star.text.TextTable")
                table.initialize(rows, columns)
                if parent:
                    parent_table = _get_table(doc, parent)
                    names = _writer_named_cells(parent_table)
                    host_cell_name = _resolve_cell_name(cell_raw, names) or ""
                    if not host_cell_name:
                        raise ValueError(
                            _unknown_cell_message(cell_raw, parent, names)
                        )
                    host = parent_table.getCellByName(host_cell_name)
                    # After existing cell text so setString-refuse still applies to the host.
                    host.insertTextContent(host.getEnd(), table, False)
                else:
                    text = doc.getText()
                    cursor = request_selection_range(ctx)
                    if cursor is None:
                        cursor = text.getEnd()
                    try:
                        text.insertTextContent(cursor, table, False)
                    except Exception as exc:
                        if is_disposal(exc):
                            raise
                        # Body XText + cursor already in a cell: do not guess a nest target.
                        # doc.getText().insertTextContent(cursor, table) raises when the
                        # cursor already sits in a nested XText (table cell, frame):
                        # "End of content node doesn't have the proper start node".
                        msg = str(exc).lower()
                        if "start node" in msg and "content node" in msg:
                            raise ValueError(
                                "Cannot insert a table at the cursor (it is probably inside a cell). "
                                "Pass parent and cell to nest, or move the cursor out of the table."
                            )
                        raise

                if data:
                    written_cells = fill_table_cells(table, data)

                created_name = uno_read(lambda: str(table.getName()), "")

            uno_ctx = resolve_uno_context(ctx)
            run_writer_mutation_with_optional_review(doc, uno_ctx, _apply)

            return {
                "status": "ok",
                "message": "Table inserted",
                "table_name": created_name,
                "rows": rows,
                "columns": columns,
                "cells_written": written_cells,
                "nesting": {"is_nested": True, "parent_table": parent, "parent_cell": host_cell_name} if parent else _not_nested(),
            }
        except ValueError as ve:
            return self._tool_error(str(ve))


class TableDelete(ToolWriterTableBase):
    name: str | None = "table_delete"
    description: str = (
        "Delete a table by name. Writer: removes a top-level or nested TextTable from its "
        "containing XText. Nested children of the deleted table are removed with it — this is "
        "the intentional remove (do not use table_set_cell, which refuses host cells). "
        "Refuses a table that has, or is nested in, a protected cell. "
        "Draw/Impress: removes the TableShape from the page."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Table name from table_list."},
            "page": _PAGE_PROPERTY,
            "index": {"type": "integer", "description": "Draw/Impress: shape index on the page."},
        },
        "required": [],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = str(kwargs.get("name") or "").strip()
        try:
            if is_draw(ctx.doc):
                # Lazy: cross-app; Draw table helpers load only when the target is a Draw/Impress document.
                from plugin.draw.tables import delete_draw_table

                result = delete_draw_table(
                    ctx.doc, name=name, page=kwargs.get("page"), index=kwargs.get("index")
                )
                return _raise_draw_result(result, "Delete failed")
            table = _require_table(ctx.doc, name)
            nesting = _nesting_for(ctx.doc, table, name)
            uno_ctx = resolve_uno_context(ctx)

            def _apply() -> bool:
                # In review mode the wrapper below has just turned change tracking on; a user
                # who tracks changes by hand has it on already. Either way the deletion must
                # be recorded, not silently applied.
                return bool(delete_writer_table(ctx.doc, uno_ctx, table, name, nesting))

            tracked = bool(_mutate_or_rollback(ctx.doc, uno_ctx, "WriterAgent: Delete table", _apply))
            result = {
                "status": "ok",
                "message": "Table deleted",
                "table_name": name,
                "nesting": nesting,
            }
            if tracked:
                result["message"] = (
                    "Table marked for deletion as a tracked change: it stays in the document, "
                    "struck through, until the user accepts the change. Do not accept or "
                    "reject it yourself."
                )
                flag_tracked(result)
            return result
        except ValueError as ve:
            return self._tool_error(str(ve))
