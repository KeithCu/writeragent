# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Table tools: Writer XTextTable and Draw/Impress TableShape (same names, different UNO).

Writer: named text tables (table_list / getCellByName). Draw: TableShape on a page
(page + shape index, or shape.Name). Cell text is PLAIN (not tracked changes).
"""
import logging
from typing import Any

from plugin.doc.text_helpers import get_string_without_tracked_deletions
from plugin.framework.errors import is_disposed_exception, make_tool_error, uno_probe

from ..range_copy import _writer_cell_position  # LibrePy-shipped; do not invert
from ..specialized_base import ToolWriterTableBase, enum_param
from ..table_helpers import edit_table_band, fill_table_cells, table_dims
from ..table_nesting import (
    _cell_hosted_table_names,
    cell_plain_siblings,
    set_host_paragraphs,
)  # LibrePy-shipped; do not invert
from plugin.framework.uno_context import resolve_uno_context, uno_same

log = logging.getLogger("writeragent.writer.specialized.tables")

# The zero-width joiner Writer drops into an empty row to anchor its tracked deletion.
_EMPTY_ROW_ANCHOR = "\u200d"

# uno_probe default for sites where None is a real UNO result.
_FAILED: Any = object()


def _is_draw_doc(doc: Any) -> bool:
    """True for Draw/Impress. Mocks without supportsService are treated as Writer."""

    def probe() -> bool:
        ss = getattr(doc, "supportsService", None)
        return callable(ss) and bool(ss("com.sun.star.drawing.DrawingDocument") or ss("com.sun.star.presentation.PresentationDocument"))

    return uno_probe(probe, False)


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


def _resolve_cell_name(raw: str, names: list[str]) -> str | None:
    """Match a user-supplied cell address against the table's REAL cell names.

    Exact match first, then the uppercased form — NEVER a blind upper rewrite: on a >26-column
    table 'a1' (Writer's real name for column 27) and 'A1' are DIFFERENT cells, and upping the
    input would silently write the wrong one."""
    name_set = set(names)
    if raw in name_set:
        return raw
    up = raw.upper()
    if up in name_set:
        return up
    return None


def _writer_named_cells(table: Any) -> list[str]:
    """Address list from ``getCellNames()`` — not ``range(cols)`` from ``getColumns()``.

    ``getColumns()`` is the first row's box count (``SwXTableColumns::getCount``;
    the ``IsTableComplex`` guard above that return is commented out). After
    merging A1:D1 on a 5×4 table it reports 1, so ``range(cols)`` skipped D2.
    """
    return list(uno_probe(lambda: table.getCellNames(), []) or ())


def _cells_sample(names: list[str]) -> str:
    """Short listing for the shared 'Its cells are: …' error (set/insert/get)."""
    if not names:
        return "none"
    if len(names) <= 8:
        return ", ".join(names)
    return "%s, …, %s" % (", ".join(names[:8]), names[-1])


def _unknown_cell_message(cell_raw: str, table_name: str, names: list[str]) -> str:
    return "Cell '%s' not in table '%s'. Its cells are: %s." % (
        cell_raw,
        table_name,
        _cells_sample(names),
    )


def _cell_matrix_text(cell: Any, visible: bool = True, hosted_tables: list[str] | None = None) -> str:
    """Cell text for table_get_cells: host cells omit concatenated inner-table text.

    *visible* hides pending tracked deletions: getString() also returns text a tracked change
    deleted, so a pending "velha" -> "nova" read "celula novavelha" here while
    get_document_content showed "celula nova" (relato #43).
    """
    has_hosted = bool(hosted_tables) if hosted_tables is not None else bool(_cell_hosted_table_names(cell))
    if has_hosted:
        return cell_plain_siblings(cell)
    return get_string_without_tracked_deletions(cell) if visible else cell.getString()


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
        parent = uno_probe(lambda: tables.getByName(parent_name), _FAILED)
        cell_names = _FAILED if parent is _FAILED else uno_probe(lambda: parent.getCellNames(), _FAILED)
        if cell_names is _FAILED:
            continue
        cell_counts[parent_name] = len(cell_names)
        for cell_name in cell_names:
            cell = uno_probe(lambda: parent.getCellByName(cell_name), _FAILED)
            if cell is _FAILED:
                continue
            children = _cell_hosted_table_names(cell)
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
    return uno_probe(probe, None)


def _nesting_for(doc: Any, table: Any, name: str) -> dict[str, Any]:
    """Direct-parent nesting dict for one table (full cell walk only when nested)."""
    parent = _table_parent(doc, table)
    if parent is not None:
        return parent
    return _writer_nesting(doc)[0].get(name, _not_nested())


def _is_wrong_start_node(exc: BaseException) -> bool:
    """True for the body-XText + cursor-in-cell insert failure.

    ``doc.getText().insertTextContent(viewCursor, table)`` raises when the
    cursor already sits in a nested XText (table cell, frame). The usual
    wording is ``End of content node doesn't have the proper start node``.
    """
    msg = str(exc).lower()
    return "start node" in msg and "content node" in msg


def _recording_changes(doc: Any) -> bool:
    return uno_probe(lambda: bool(doc.getPropertyValue("RecordChanges")), False)


def _delete_writer_table_tracked(doc: Any, uno_ctx: Any, table: Any, name: str) -> None:
    """Delete a Writer table as a tracked change. Change tracking must already be on.

    Removing the table with removeTextContent while change tracking
    is on -- the agent's review mode records every edit -- produces
    no redline. The table vanishes, the user has nothing to review
    or reject, and tracked changes still pending inside it vanish
    with it. Neither removeTextContent / dispose nor removing every
    row is recorded (checked on LibreOffice 26.2: 0 redlines each
    way). Selecting the table and running .uno:DeleteTable -- the
    UI's own delete -- is recorded as a tracked deletion; the table
    stays, struck through, until the change is accepted.

    Empty rows need one more step. Writer records a row deletion as the deletion of the
    row's text, and for a row with none it inserts a U+200D anchor into the row -- but
    records no redline for it (26.2, via the API and via .uno:TrackChanges alike). So
    accepting left the empty rows behind as a table, and an all-empty table recorded
    nothing. Deleting each such anchor with tracking on gives every row its redline;
    accepting then removes the whole table. Rejecting keeps the anchor in that empty cell,
    as LibreOffice's own Delete Table does.

    Fails closed: without a new redline this raises instead of reporting an unreviewable
    deletion as done.
    """
    controller = doc.getCurrentController()
    if controller is None:
        raise RuntimeError("no document view to delete the table through")
    empty_cells = [n for n in table.getCellNames() if not table.getCellByName(n).getString()]
    previous = uno_probe(lambda: controller.getSelection(), None)
    before = doc.getRedlines().getCount()
    controller.select(table)
    try:
        helper = uno_ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.DispatchHelper", uno_ctx)
        helper.executeDispatch(controller.getFrame(), ".uno:DeleteTable", "", 0, ())
        try:
            for cell_name in empty_cells:
                cell = table.getCellByName(cell_name)
                if cell.getString() == _EMPTY_ROW_ANCHOR:
                    cursor = cell.createTextCursor()
                    cursor.gotoStart(False)
                    cursor.goRight(1, True)
                    cursor.setString("")
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.debug("table_delete: could not clear an empty-row anchor", exc_info=True)
    finally:
        if previous is not None:
            try:
                controller.select(previous)  # leave the user's cursor where it was
            except Exception as exc:
                if is_disposed_exception(exc):
                    raise
                log.debug("table_delete: could not restore the previous selection", exc_info=True)

    if doc.getRedlines().getCount() > before:
        return
    if doc.getTextTables().hasByName(name):
        raise RuntimeError("the tracked delete did not run, so the table was left in place")
    raise RuntimeError("the table was removed but no tracked change was recorded")


def delete_writer_table(doc: Any, uno_ctx: Any, table: Any, name: str, nesting: dict[str, Any]) -> bool:
    """Remove *table*. True when the deletion was recorded as a tracked change.

    ``table_delete`` and ``apply_document_content`` both call this. The caller
    wraps it in the EditReviewSession it already has, so RecordChanges is on
    before the tracked path runs. A second review wrapper here would nest
    sessions and tag the redlines twice.
    """
    if _recording_changes(doc):
        _delete_writer_table_tracked(doc, uno_ctx, table, name)
        return True
    _remove_writer_table(doc, table, name, nesting)
    return False


def record_table_deletions(session: Any, doc: Any, ctx: Any, doomed: list[tuple[Any, str]]) -> bool:
    """Delete tables inside the caller's already-open review session.

    Returns True when at least one deletion was a tracked change. Does not
    open a second EditReviewSession: apply_document_content is already in
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
        if is_disposed_exception(exc):
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

    Scan ``getCellNames()`` and keep names on that row or column. The old
    ``range(cols)`` walk used ``getColumns()`` (first-row box count) and missed
    D2 after an A1:D1 merge. Column index uses the Writer base-52 parser, not
    spreadsheet ``parse_a1``.
    """
    hosted: list[str] = []
    for cell_name in _writer_named_cells(table):
        pos = _writer_cell_position(cell_name)
        if pos is None:
            continue
        col_idx, row_idx = pos
        if axis_arg == "row":
            if row_idx != idx:
                continue
        elif col_idx != idx:
            continue
        cell = uno_probe(lambda: table.getCellByName(cell_name), _FAILED)
        if cell is not _FAILED:
            hosted.extend(_cell_hosted_table_names(cell))
    # Preserve first-seen order (a band can host more than one nested table).
    seen: list[str] = []
    for name in hosted:
        if name not in seen:
            seen.append(name)
    return seen


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
        # execute_safe already returns TOOL_EXECUTION_ERROR; this did not name a table.
        if _is_draw_doc(ctx.doc):
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
                # One bad or corrupt table must not fail the entire table_list
                # call. _dims(tables.getByName(name)) raises inside the iteration.
                # Catch per-table errors, re-raise document disposal, log a
                # warning, and skip.
                if is_disposed_exception(exc):
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
            "page": {"type": "integer", "description": "Draw/Impress: 0-based page index."},
            "index": {"type": "integer", "description": "Draw/Impress: shape index on the page."},
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = str(kwargs.get("name") or "").strip()
        cell_raw = str(kwargs.get("cell") or "").strip()
        try:
            if _is_draw_doc(ctx.doc):
                from plugin.draw.tables import get_draw_cell, get_draw_cells, resolve_draw_table

                entry = resolve_draw_table(ctx.doc, name=name, page=kwargs.get("page"), index=kwargs.get("index"))
                payload = {
                    "status": "ok",
                    "table_name": entry.get("name") or name,
                    "page": entry.get("page"),
                    "index": entry.get("index"),
                    "rows": entry.get("rows"),
                    "cols": entry.get("cols"),
                }
                if cell_raw:
                    text = get_draw_cell(entry, cell_raw)
                    payload["cell"] = cell_raw
                    payload["matrix"] = [[text]]
                else:
                    payload["matrix"] = get_draw_cells(entry)
                return payload
            if not name:
                return self._tool_error("name is required.")
            table = _get_table(ctx.doc, name)
            rows, cols = table_dims(table)
            # getColumns() is the first row's box count (SwXTableColumns::getCount),
            # so range(cols) skipped D2 after A1:D1 merge. getCellNames() is the list.
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

            cells: dict[str, str] = {}
            nested_in_cells: dict[str, list[str]] = {}
            for cell_name in cell_names:
                try:
                    c = table.getCellByName(cell_name)
                    hosted = _cell_hosted_table_names(c)
                    if hosted:
                        nested_in_cells[cell_name] = hosted
                    cells[cell_name] = _cell_matrix_text(c, hosted_tables=hosted)
                except Exception as exc:
                    # Per-cell read errors must not disappear into a bare continue.
                    # That also swallows document disposal. Re-raise disposal and log
                    # the cell read failure.
                    if is_disposed_exception(exc):
                        raise
                    log.warning("table_get_cells: could not read cell '%s' in table '%s': %s", cell_name, name, exc)
                    continue
            if cell_raw and (not cell_names or cell_names[0] not in cells):
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
            if cell_raw:
                payload["cell"] = resolved
            return payload
        except ValueError as ve:
            return self._tool_error(str(ve))


class TableSetCell(ToolWriterTableBase):
    name: str | None = "table_set_cell"
    description: str = (
        "Set the plain-text content of ONE table cell, addressed A1-style (e.g. 'B2'). "
        "A normal cell is replaced with setString (clears in-cell formatting). "
        "A cell that hosts a nested table keeps that table and rewrites only the host "
        "paragraphs. Edit the nested table by its own name. "
        "Not a tracked change even when review mode is on."
    )
    is_mutation: bool | None = True
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Table name from table_list."},
            "cell": {"type": "string", "description": "A1-style cell address, e.g. 'B2'."},
            "text": {"type": "string", "description": "New plain text for the cell."},
            "page": {"type": "integer", "description": "Draw/Impress: 0-based page index."},
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
            if _is_draw_doc(ctx.doc):
                from plugin.draw.tables import resolve_draw_table, set_draw_cell

                entry = resolve_draw_table(ctx.doc, name=name, page=kwargs.get("page"), index=kwargs.get("index"))
                old, new = set_draw_cell(entry, cell_raw, str(text))
                return {
                    "status": "ok",
                    "table_name": entry.get("name") or name,
                    "page": entry.get("page"),
                    "index": entry.get("index"),
                    "cell": cell_raw,
                    "old_text": old,
                    "new_text": new,
                }
            if not name:
                return self._tool_error("name is required.")
            table = _get_table(ctx.doc, name)
            names = _writer_named_cells(table)
            cell_name = _resolve_cell_name(cell_raw, names)
            if cell_name is None:
                return self._tool_error(
                    _unknown_cell_message(cell_raw, name, names)
                )
            cell = table.getCellByName(cell_name)
            # setString wipes nested TextTables. Rewrite host paragraphs instead.
            nested = _cell_hosted_table_names(cell)
            if nested:
                old = cell_plain_siblings(cell)
                set_host_paragraphs(cell, str(text))
                return {
                    "status": "ok",
                    "table_name": name,
                    "cell": cell_name,
                    "old_text": old,
                    "new_text": str(text),
                    "nested_tables": nested,
                }
            old = cell.getString()
            cell.setString(str(text))
            return {"status": "ok", "table_name": name, "cell": cell_name, "old_text": old, "new_text": str(text)}
        except ValueError as ve:
            return self._tool_error(str(ve))


class ManageTableStructure(ToolWriterTableBase):
    """Insert or delete one row/column. The four former skinny tools shared table_name + index."""

    name: str | None = "manage_table_structure"
    description: str = (
        "Insert or delete one table row or column. "
        "index is 0-based (for insert, equal to the current count appends at the end). "
        "Cannot delete the last remaining row or column. "
        "Writer: refuses delete if the row/column hosts a nested table (that would destroy it)."
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
            "page": {"type": "integer", "description": "Draw/Impress: 0-based page index."},
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
        axis = "rows" if axis_arg == "row" else "columns"
        insert = action == "insert"
        try:
            if _is_draw_doc(ctx.doc):
                from plugin.draw.tables import manage_draw_structure, resolve_draw_table

                entry = resolve_draw_table(
                    ctx.doc,
                    name=name,
                    page=kwargs.get("page"),
                    index=kwargs.get("shape_index"),
                )
                rows, cols = manage_draw_structure(entry, action, axis_arg, idx)
                return {
                    "status": "ok",
                    "table_name": entry.get("name") or name,
                    "page": entry.get("page"),
                    "index": entry.get("index"),
                    "rows": rows,
                    "cols": cols,
                }
            if not name:
                return self._tool_error("name is required.")
            table = _get_table(ctx.doc, name)
            band = table.getRows() if axis == "rows" else table.getColumns()
            count = band.getCount()
            if not insert and idx < count and count > 1:
                nested = _hosted_in_band(table, axis_arg, idx)
                if nested:
                    return self._tool_error(
                        "Cannot delete this %s: it contains nested table(s) %s. "
                        "Edit those tables by name first."
                        % (axis_arg, ", ".join(nested))
                    )
            edit_table_band(band, str(action), idx, axis)
            rows, cols = table_dims(table)
            return {"status": "ok", "table_name": name, "rows": rows, "cols": cols}
        except ValueError as ve:
            return self._tool_error(str(ve))


class TableInsert(ToolWriterTableBase):
    name: str | None = "table_insert"
    intent: str | None = "edit"
    description: str = (
        "Insert a table. Writer: text table at the view cursor (or document end); "
        "pass parent + cell to nest inside an existing table cell (inserts at the cell end). "
        "Draw/Impress: TableShape; position/size in 1/100 mm. parent/cell are Writer-only. "
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
            "page": {"type": "integer", "description": "Draw/Impress: 0-based page index (active if omitted)"},
            "x": {"type": "integer", "description": "Draw/Impress: X in 1/100 mm (default: 3000)"},
            "y": {"type": "integer", "description": "Draw/Impress: Y in 1/100 mm (default: 4000)"},
            "width": {"type": "integer", "description": "Draw/Impress: width in 1/100 mm (default: 20000)"},
            "height": {"type": "integer", "description": "Draw/Impress: height in 1/100 mm (default: 10000)"},
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
            if _is_draw_doc(ctx.doc):
                from plugin.draw.tables import insert_draw_table

                if parent or cell_raw:
                    return self._tool_error("parent and cell are Writer-only (Draw has no nested text tables).")
                result = insert_draw_table(ctx, **kwargs)
                if result.get("status") != "ok":
                    return make_tool_error(str(result.get("message") or "Insert failed"), code=str(result.get("code") or "TOOL_EXECUTION_ERROR"))
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
                if len(data) > rows:
                    return self._tool_error("data has %d rows but table only has %d." % (len(data), rows))
                for r_idx, row in enumerate(data):
                    if isinstance(row, (list, tuple)) and len(row) > columns:
                        return self._tool_error("data row %d has %d columns but table only has %d." % (r_idx, len(row), columns))

            doc = ctx.doc
            host_cell_name = ""
            written_cells = 0
            created_name = ""
            created_nesting = _not_nested()

            def _apply() -> None:
                nonlocal host_cell_name, written_cells, created_name, created_nesting
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
                    cursor = uno_probe(lambda: doc.getCurrentController().getViewCursor(), None)
                    if cursor is None:
                        cursor = text.getEnd()
                    try:
                        text.insertTextContent(cursor, table, False)
                    except Exception as exc:
                        if is_disposed_exception(exc):
                            raise
                        # Body XText + cursor already in a cell: do not guess a nest target.
                        if _is_wrong_start_node(exc):
                            raise ValueError(
                                "Cannot insert a table at the view cursor (it is probably inside a cell). "
                                "Pass parent and cell to nest, or move the cursor out of the table."
                            )
                        raise

                if data:
                    written_cells = fill_table_cells(table, data)

                created_name = uno_probe(lambda: str(table.getName() if hasattr(table, "getName") else getattr(table, "Name", "") or ""), created_name)

                if parent and host_cell_name:
                    created_nesting = {"is_nested": True, "parent_table": parent, "parent_cell": host_cell_name}
                else:
                    created_nesting = _not_nested()

            uno_ctx = resolve_uno_context(ctx)
            from plugin.writer.format import run_writer_mutation_with_optional_review
            run_writer_mutation_with_optional_review(doc, uno_ctx, _apply)

            return {
                "status": "ok",
                "message": "Table inserted",
                "table_name": created_name,
                "rows": rows,
                "columns": columns,
                "cells_written": written_cells,
                "nesting": created_nesting,
            }
        except ValueError as ve:
            return self._tool_error(str(ve))


class TableDelete(ToolWriterTableBase):
    name: str | None = "table_delete"
    intent: str | None = "edit"
    description: str = (
        "Delete a table by name. Writer: removes a top-level or nested TextTable from its "
        "containing XText. Nested children of the deleted table are removed with it — this is "
        "the intentional remove (do not use table_set_cell, which refuses host cells). "
        "Draw/Impress: removes the TableShape from the page."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Table name from table_list."},
            "page": {"type": "integer", "description": "Draw/Impress: 0-based page index."},
            "index": {"type": "integer", "description": "Draw/Impress: shape index on the page."},
        },
        "required": [],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = str(kwargs.get("name") or "").strip()
        try:
            if _is_draw_doc(ctx.doc):
                from plugin.draw.tables import delete_draw_table

                result = delete_draw_table(
                    ctx.doc, name=name, page=kwargs.get("page"), index=kwargs.get("index")
                )
                if result.get("status") != "ok":
                    return make_tool_error(
                        str(result.get("message") or "Delete failed"),
                        code=str(result.get("code") or "TOOL_EXECUTION_ERROR"),
                    )
                return result
            if not name:
                return self._tool_error("name is required.")
            table = _get_table(ctx.doc, name)
            nesting = _nesting_for(ctx.doc, table, name)
            tracked: list[bool] = []
            uno_ctx = resolve_uno_context(ctx)

            def _apply() -> None:
                # In review mode the wrapper below has just turned change tracking on; a user
                # who tracks changes by hand has it on already. Either way the deletion must
                # be recorded, not silently applied.
                if delete_writer_table(ctx.doc, uno_ctx, table, name, nesting):
                    tracked.append(True)

            from plugin.writer.format import run_writer_mutation_with_optional_review

            run_writer_mutation_with_optional_review(ctx.doc, uno_ctx, _apply)
            if tracked:
                return {
                    "status": "ok",
                    "message": ("Table marked for deletion as a tracked change: it stays in the document, "
                                "struck through, until the user accepts the change. Do not accept or "
                                "reject it yourself."),
                    "table_name": name,
                    "nesting": nesting,
                    "pending_review": True,
                }
            return {
                "status": "ok",
                "message": "Table deleted",
                "table_name": name,
                "nesting": nesting,
            }
        except ValueError as ve:
            return self._tool_error(str(ve))
