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

from ..specialized_base import ToolWriterTableBase

log = logging.getLogger("writeragent.writer.specialized.tables")


def _is_draw_doc(doc: Any) -> bool:
    """True for Draw/Impress. Mocks without supportsService are treated as Writer."""
    try:
        ss = getattr(doc, "supportsService", None)
        if not callable(ss):
            return False
        return bool(
            ss("com.sun.star.drawing.DrawingDocument") or ss("com.sun.star.presentation.PresentationDocument")
        )
    except Exception:
        return False


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


def _dims(table: Any) -> tuple[int, int]:
    return int(table.getRows().getCount()), int(table.getColumns().getCount())


def _col_letters(col_idx: int) -> str:
    """0-based column index -> spreadsheet letters (0->A, 25->Z, 26->AA).

    NOTE: Writer's OWN naming diverges past column Z (it continues with lowercase a..z, not AA),
    so this is only used for the fallback read path and the <=26-column range hint. Reads prefer
    getCellByPosition, and table_set_cell validates against the table's REAL getCellNames()."""
    s = ""
    n = col_idx
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def _cell_name(col_idx: int, row_idx: int) -> str:
    """0-based (col, row) -> A1-style name (col 0/row 0 -> 'A1'). See _col_letters caveat."""
    return "%s%d" % (_col_letters(col_idx), row_idx + 1)


def _resolve_cell_name(table: Any, raw: str) -> str | None:
    """Match a user-supplied cell address against the table's REAL cell names.

    Exact match first, then the uppercased form — NEVER a blind upper rewrite: on a >26-column
    table 'a1' (Writer's real name for column 27) and 'A1' are DIFFERENT cells, and upping the
    input would silently write the wrong one."""
    names = set(table.getCellNames())
    if raw in names:
        return raw
    up = raw.upper()
    if up in names:
        return up
    return None


def _service_named(obj: Any, name: str) -> bool:
    """True if *obj* supports *name*. Prefer supportsService; fall back to names."""
    try:
        ss = getattr(obj, "supportsService", None)
        if callable(ss):
            return bool(ss(name))
    except Exception:
        pass
    try:
        return name in (obj.getSupportedServiceNames() or ())
    except Exception:
        return False


def _is_text_table(obj: Any) -> bool:
    """True for a Writer TextTable."""
    return _service_named(obj, "com.sun.star.text.TextTable")


def _container_xtext(obj: Any) -> Any | None:
    """Inner XText of a TextFrame or TextSection, else None.

    A table inside one of these still lives in the host cell — setString on
    the cell would destroy it — so discovery walks through the container.
    """
    if not (
        _service_named(obj, "com.sun.star.text.TextFrame")
        or _service_named(obj, "com.sun.star.text.TextSection")
    ):
        return None
    try:
        inner = obj.getText() if hasattr(obj, "getText") else obj
    except Exception:
        inner = obj
    return inner


def _iter_direct_children(xtext: Any):
    """Yield each element of *xtext*'s XEnumeration (one level, no recurse)."""
    try:
        enum = xtext.createEnumeration()
    except Exception:
        return
    while True:
        try:
            if not enum.hasMoreElements():
                break
            yield enum.nextElement()
        except Exception:
            return


def _iter_text_frames_in_para(para: Any):
    """As-character TextFrames in *para* (portion property ``TextFrame``).

    Probed: a frame in a cell is not an XEnumeration sibling — the cell enum
    is only Paragraph; the frame hangs off a portion.
    """
    try:
        enum = para.createEnumeration()
    except Exception:
        return
    while True:
        try:
            if not enum.hasMoreElements():
                break
            portion = enum.nextElement()
        except Exception:
            return
        try:
            frame = portion.getPropertyValue("TextFrame")
        except Exception:
            continue
        if frame is not None:
            yield frame


def _walk_xtext_siblings(xtext: Any, *, into_containers: bool = True):
    """Yield ``('table', obj)`` or ``('para', obj)`` from *xtext*.

    When *into_containers* is true (cell / body), follow TextFrame /
    TextSection siblings and as-character ``TextFrame`` portions. Once
    inside a container, leave it false so the frame's own portions do not
    re-enter the same frame (UNO hands back a new proxy each time).
    """
    for element in _iter_direct_children(xtext):
        if _is_text_table(element):
            yield ("table", element)
            continue
        if into_containers:
            inner = _container_xtext(element)
            if inner is not None:
                yield from _walk_xtext_siblings(inner, into_containers=False)
                continue
            for frame in _iter_text_frames_in_para(element):
                nested = _container_xtext(frame)
                if nested is not None:
                    yield from _walk_xtext_siblings(nested, into_containers=False)
        yield ("para", element)


def _cell_hosted_table_names(cell: Any) -> list[str]:
    """Names of TextTables hosted in *cell* (direct or via frame/section).

    Writer nests a TextTable as a sibling of the cell's paragraphs — not via
    anchors or geometry. Empty if the cell cannot be enumerated (plain fakes,
    covered/merged cells). Used by set/delete guards so they scan one cell,
    not the whole document.
    """
    names: list[str] = []
    for kind, obj in _walk_xtext_siblings(cell):
        if kind != "table":
            continue
        try:
            nested_name = str(obj.getName() or "")
        except Exception:
            nested_name = ""
        if nested_name:
            names.append(nested_name)
    return names


def _cell_plain_siblings(cell: Any) -> str:
    """Host-cell paragraph text only — skips nested tables' getString() dump."""
    parts: list[str] = []
    for kind, obj in _walk_xtext_siblings(cell):
        if kind != "para":
            continue
        try:
            part = obj.getString()
        except Exception:
            part = ""
        if part:
            parts.append(str(part))
    return "\n".join(parts)


def _cell_matrix_text(cell: Any) -> str:
    """Cell text for table_get_cells: host cells omit concatenated inner-table text."""
    if _cell_hosted_table_names(cell):
        return _cell_plain_siblings(cell)
    try:
        return cell.getString()
    except Exception:
        return ""


def _set_host_paragraphs(cell: Any, text: str) -> None:
    """Rewrite the cell's own paragraph siblings; leave tables and frames.

    Direct children only — do not rewrite text inside a frame/section (that
    would be a different XText). No paragraphs: insert at getStart() so a
    caption lands before a table that table_insert placed at getEnd().
    """
    paras: list[Any] = []
    for element in _iter_direct_children(cell):
        if _is_text_table(element) or _container_xtext(element) is not None:
            continue
        paras.append(element)
    if not paras:
        cell.insertString(cell.getStart(), text, False)
        return
    paras[0].setString(text)
    for extra in paras[1:]:
        extra.setString("")


def range_hosted_nested_tables(text_range: Any) -> list[str]:
    """Nested table names that setString on *text_range* would destroy.

    Only when the range lives inside a table cell (cursor ``TextTable`` is
    set). Body XText tables are top-level — a body search-replace must not
    be treated as a host-cell wipe.
    """
    try:
        text_obj = text_range.getText()
        cur = text_obj.createTextCursorByRange(text_range.getStart())
        if cur.getPropertyValue("TextTable") is None:
            return []
    except Exception:
        return []
    return _cell_hosted_table_names(text_obj)


def raise_if_range_hosts_nested_table(text_range: Any) -> None:
    """Refuse a rewrite that would setString a host cell (wipes nested tables)."""
    hosted = range_hosted_nested_tables(text_range)
    if not hosted:
        return
    from plugin.framework.errors import ToolExecutionError

    raise ToolExecutionError(
        "This range is in a table cell that contains nested table(s) %s. "
        "Use table_set_cell for the host cell's own paragraphs, or "
        "edit those tables by name — apply_document_content would delete them."
        % ", ".join(hosted)
    )


def _not_nested() -> dict[str, Any]:
    return {"is_nested": False, "parent_table": None, "parent_cell": None}


def _writer_nesting(doc: Any) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, list[str]]]]:
    """One pass: child name -> nesting dict, parent name -> {cell: [child names]}.

    Direct parent only — a grandchild reports the mid table, not the outer.
    """
    tables = _tables(doc)
    names = list(tables.getElementNames())
    child_nesting: dict[str, dict[str, Any]] = {}
    hosted: dict[str, dict[str, list[str]]] = {}

    for parent_name in names:
        try:
            parent = tables.getByName(parent_name)
            cell_names = parent.getCellNames()
        except Exception:
            continue
        for cell_name in cell_names:
            try:
                cell = parent.getCellByName(cell_name)
            except Exception:
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
    return child_nesting, hosted


def _nesting_for(doc: Any, name: str) -> dict[str, Any]:
    """Direct-parent nesting dict for one table name (missing -> not nested)."""
    nesting_by_name = _writer_nesting(doc)[0]
    return nesting_by_name.get(name, _not_nested())


def _is_wrong_start_node(exc: BaseException) -> bool:
    """True for the body-XText + cursor-in-cell insert failure.

    ``doc.getText().insertTextContent(viewCursor, table)`` raises when the
    cursor already sits in a nested XText (table cell, frame). The usual
    wording is ``End of content node doesn't have the proper start node``.
    """
    msg = str(exc).lower()
    return "start node" in msg or "content node" in msg


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
    except Exception:
        log.debug("table.getAnchor() remove failed for '%s'; using nesting fallback", name, exc_info=True)
    if nesting.get("is_nested"):
        parent = _get_table(doc, str(nesting.get("parent_table") or ""))
        host = parent.getCellByName(str(nesting.get("parent_cell") or ""))
        host.removeTextContent(table)
        return
    doc.getText().removeTextContent(table)


def _hosted_in_band(table: Any, axis_arg: str, idx: int) -> list[str]:
    """Nested table names hosted in the row/column about to be deleted.

    Local to that band (getCellByPosition, then the computed A1 name).
    """
    rows, cols = _dims(table)
    hosted: list[str] = []
    coords = ((c, idx) for c in range(cols)) if axis_arg == "row" else ((idx, r) for r in range(rows))
    for c, r in coords:
        cell = None
        try:
            cell = table.getCellByPosition(c, r)
        except Exception:
            try:
                cell = table.getCellByName(_cell_name(c, r))
            except Exception:
                continue
        hosted.extend(_cell_hosted_table_names(cell))
    # Preserve first-seen order (a band can host more than one nested table).
    seen: list[str] = []
    for name in hosted:
        if name not in seen:
            seen.append(name)
    return seen


class TableList(ToolWriterTableBase):
    name = "table_list"
    description = (
        "List tables with name and dimensions (rows x columns). Writer: text-table names; each "
        "row includes nesting (direct parent table/cell) and nested_in_cells (host cells that "
        "contain nested tables — table_set_cell keeps those tables; do not delete the host row/column). "
        "Draw/Impress: also page and shape index."
    )
    is_mutation = False
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            if _is_draw_doc(ctx.doc):
                from plugin.draw.tables import list_draw_tables

                out = list_draw_tables(ctx.doc)
                return {"status": "ok", "count": len(out), "tables": out}
            tables = _tables(ctx.doc)
            nesting_by_name, hosted = _writer_nesting(ctx.doc)
            out = []
            for name in tables.getElementNames():
                rows, cols = _dims(tables.getByName(name))
                out.append({
                    "name": name,
                    "rows": rows,
                    "cols": cols,
                    "nesting": nesting_by_name.get(name, _not_nested()),
                    "nested_in_cells": hosted.get(name, {}),
                })
            return {"status": "ok", "count": len(out), "tables": out}
        except Exception as e:
            log.exception("Could not list tables")
            return self._tool_error("Could not list tables: %s" % e)


class TableGetCells(ToolWriterTableBase):
    name = "table_get_cells"
    description = (
        "Return a table's cell text as a row-major matrix (matrix[row][col]) by position — not by "
        "cell name. Writer host cells that contain nested tables return only the host cell's own "
        "paragraphs (not concatenated inner-table text); nested_in_cells names those cells. "
        "Use matrix indices to read values; for table_set_cell use Writer cell names from "
        "table_set_cell error hints (columns A..Z then a..z past column 26, not spreadsheet AA). "
        "table_set_cell on a host cell updates that host text and leaves nested tables."
    )
    is_mutation = False
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Table name from table_list."},
            "page": {"type": "integer", "description": "Draw/Impress: 0-based page index."},
            "index": {"type": "integer", "description": "Draw/Impress: shape index on the page."},
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = str(kwargs.get("name") or "").strip()
        try:
            if _is_draw_doc(ctx.doc):
                from plugin.draw.tables import get_draw_cells, resolve_draw_table

                entry = resolve_draw_table(ctx.doc, name=name, page=kwargs.get("page"), index=kwargs.get("index"))
                matrix = get_draw_cells(entry)
                return {
                    "status": "ok",
                    "table_name": entry.get("name") or name,
                    "page": entry.get("page"),
                    "index": entry.get("index"),
                    "rows": entry.get("rows"),
                    "cols": entry.get("cols"),
                    "matrix": matrix,
                }
            if not name:
                return self._tool_error("name is required.")
            table = _get_table(ctx.doc, name)
            rows, cols = _dims(table)
            matrix = []
            for r in range(rows):
                row = []
                for c in range(cols):
                    # Prefer position-based access (naming-scheme-proof: Writer names columns
                    # A..Z then lowercase a..z); fall back to the computed A1 name, then blank
                    # (merged/covered cells have no addressable cell).
                    val = ""
                    try:
                        val = _cell_matrix_text(table.getCellByPosition(c, r))
                    except Exception:
                        try:
                            val = _cell_matrix_text(table.getCellByName(_cell_name(c, r)))
                        except Exception:
                            val = ""
                    row.append(val)
                matrix.append(row)
            nesting_by_name, hosted = _writer_nesting(ctx.doc)
            return {
                "status": "ok",
                "table_name": name,
                "rows": rows,
                "cols": cols,
                "matrix": matrix,
                "nesting": nesting_by_name.get(name, _not_nested()),
                "nested_in_cells": hosted.get(name, {}),
            }
        except ValueError as ve:
            return self._tool_error(str(ve))
        except Exception as e:
            log.exception("Could not read table '%s'", name)
            return self._tool_error("Could not read table '%s': %s" % (name, e))


class TableSetCell(ToolWriterTableBase):
    name = "table_set_cell"
    description = (
        "Set the plain-text content of ONE table cell, addressed A1-style (e.g. 'B2'). "
        "A normal cell is replaced with setString (clears in-cell formatting). "
        "A cell that hosts a nested table keeps that table and rewrites only the host "
        "paragraphs. Edit the nested table by its own name. "
        "Not a tracked change even when review mode is on."
    )
    is_mutation = True
    parameters = {
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
        cell_raw = (kwargs.get("cell") or "").strip()
        text = kwargs.get("text")
        if text is None:
            return self._tool_error("text is required.")
        cell_name = cell_raw  # bound for the except below even if _get_table raises before resolution
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
            cell_name = _resolve_cell_name(table, cell_raw)
            if cell_name is None:
                names = list(table.getCellNames())
                sample = ", ".join(names[:8]) + ((", …, %s" % names[-1]) if len(names) > 8 else "")
                return self._tool_error(
                    "Cell '%s' not in table '%s'. Its cells are: %s." % (cell_raw, name, sample))
            cell = table.getCellByName(cell_name)
            # setString wipes nested TextTables. Rewrite host paragraphs instead.
            nested = _cell_hosted_table_names(cell)
            if nested:
                old = _cell_plain_siblings(cell)
                _set_host_paragraphs(cell, str(text))
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
        except Exception as e:
            log.exception("Could not set cell '%s' in table '%s'", cell_name, name)
            return self._tool_error("Could not set cell '%s' in table '%s': %s" % (cell_name, name, e))


class ManageTableStructure(ToolWriterTableBase):
    """Insert or delete one row/column. The four former skinny tools shared table_name + index."""

    name = "manage_table_structure"
    description = (
        "Insert or delete one table row or column. "
        "index is 0-based (for insert, equal to the current count appends at the end). "
        "Cannot delete the last remaining row or column. "
        "Writer: refuses delete if the row/column hosts a nested table (that would destroy it)."
    )
    is_mutation = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["insert", "delete"],
                "description": "insert or delete one band member.",
            },
            "axis": {
                "type": "string",
                "enum": ["row", "column"],
                "description": "Whether to edit rows or columns.",
            },
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
        action = kwargs.get("action")
        axis_arg = kwargs.get("axis")
        if action not in ("insert", "delete"):
            return self._tool_error("action must be 'insert' or 'delete'.")
        if axis_arg not in ("row", "column"):
            return self._tool_error("axis must be 'row' or 'column'.")
        name = str(kwargs.get("name") or "").strip()
        raw = kwargs.get("index")
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            return self._tool_error("index must be an integer.")
        try:
            idx = int(raw)
        except ValueError:
            return self._tool_error("index must be an integer.")
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
                rows, cols = manage_draw_structure(entry, str(action), str(axis_arg), idx)
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
            # insertByIndex(idx, n) inserts BEFORE idx (idx==count appends). removeByIndex needs a real
            # index, and removing the last row/column of a table is not allowed.
            if insert:
                if idx > count:
                    return self._tool_error(
                        "index %d out of range (table has %d %s; use 0..%d)."
                        % (idx, count, axis, count)
                    )
                band.insertByIndex(idx, 1)
            else:
                if idx >= count:
                    return self._tool_error(
                        "index %d out of range (table has %d %s; use 0..%d)."
                        % (idx, count, axis, count - 1)
                    )
                if count <= 1:
                    return self._tool_error("Cannot remove the last %s of a table." % axis[:-1])
                # removeByIndex destroys nested tables anchored in the deleted band.
                nested = _hosted_in_band(table, axis_arg, idx)
                if nested:
                    return self._tool_error(
                        "Cannot delete this %s: it contains nested table(s) %s. "
                        "Edit those tables by name first."
                        % (axis_arg, ", ".join(nested))
                    )
                band.removeByIndex(idx, 1)
            rows, cols = _dims(table)
            return {"status": "ok", "table_name": name, "rows": rows, "cols": cols}
        except ValueError as ve:
            return self._tool_error(str(ve))
        except Exception as e:
            log.exception("Could not edit %s of table '%s'", axis, name)
            return self._tool_error("Could not edit %s of table '%s': %s" % (axis, name, e))


class TableInsert(ToolWriterTableBase):
    name = "table_insert"
    intent = "edit"
    description = (
        "Insert a table. Writer: text table at the view cursor (or document end); "
        "pass parent + cell to nest inside an existing table cell (inserts at the cell end). "
        "Draw/Impress: TableShape; position/size in 1/100 mm. parent/cell are Writer-only. "
        "Optional data is a 2D array of cell strings."
    )
    parameters = {
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
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        parent = str(kwargs.get("parent") or "").strip()
        cell_raw = str(kwargs.get("cell") or "").strip()
        if _is_draw_doc(ctx.doc):
            from plugin.draw.tables import insert_draw_table
            from plugin.framework.errors import make_tool_error

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
        rows = kwargs.get("rows")
        columns = kwargs.get("columns")
        if rows is None or columns is None:
            return self._tool_error("rows and columns are required.")
        rows = int(rows)
        columns = int(columns)
        if rows < 1 or columns < 1:
            return self._tool_error("rows and columns must be at least 1.")
        try:
            doc = ctx.doc
            table = doc.createInstance("com.sun.star.text.TextTable")
            table.initialize(rows, columns)
            host_cell_name = ""
            if parent:
                parent_table = _get_table(doc, parent)
                host_cell_name = _resolve_cell_name(parent_table, cell_raw) or ""
                if not host_cell_name:
                    names = list(parent_table.getCellNames())
                    sample = ", ".join(names[:8]) + ((", …, %s" % names[-1]) if len(names) > 8 else "")
                    return self._tool_error(
                        "Cell '%s' not in table '%s'. Its cells are: %s." % (cell_raw, parent, sample)
                    )
                host = parent_table.getCellByName(host_cell_name)
                # After existing cell text so setString-refuse still applies to the host.
                host.insertTextContent(host.getEnd(), table, False)
            else:
                text = doc.getText()
                cursor = None
                try:
                    cursor = doc.getCurrentController().getViewCursor()
                except Exception:
                    cursor = None
                if cursor is None:
                    cursor = text.getEnd()
                try:
                    text.insertTextContent(cursor, table, False)
                except Exception as exc:
                    # Body XText + cursor already in a cell: do not guess a nest target.
                    if _is_wrong_start_node(exc):
                        return self._tool_error(
                            "Cannot insert a table at the view cursor (it is probably inside a cell). "
                            "Pass parent and cell to nest, or move the cursor out of the table."
                        )
                    raise
            written = 0
            data = kwargs.get("data")
            if data:
                from plugin.draw.tables import fill_table_cells

                written = fill_table_cells(table, data)
            name = ""
            try:
                name = str(table.getName() if hasattr(table, "getName") else getattr(table, "Name", "") or "")
            except Exception:
                pass
            # Prefer the parent/cell we just used — getTextTables() can lag a nameless insert.
            if parent and host_cell_name:
                nesting = {"is_nested": True, "parent_table": parent, "parent_cell": host_cell_name}
            elif name:
                nesting = _nesting_for(doc, name)
            else:
                nesting = _not_nested()
            return {
                "status": "ok",
                "message": "Table inserted",
                "table_name": name,
                "rows": rows,
                "columns": columns,
                "cells_written": written,
                "nesting": nesting,
            }
        except ValueError as ve:
            return self._tool_error(str(ve))
        except Exception as e:
            log.exception("Could not insert Writer table")
            return self._tool_error("Could not insert table: %s" % e)


class TableDelete(ToolWriterTableBase):
    name = "table_delete"
    intent = "edit"
    description = (
        "Delete a table by name. Writer: removes a top-level or nested TextTable from its "
        "containing XText. Nested children of the deleted table are removed with it — this is "
        "the intentional remove (do not use table_set_cell, which refuses host cells). "
        "Draw/Impress: removes the TableShape from the page."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Table name from table_list."},
            "page": {"type": "integer", "description": "Draw/Impress: 0-based page index."},
            "index": {"type": "integer", "description": "Draw/Impress: shape index on the page."},
        },
        "required": [],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = str(kwargs.get("name") or "").strip()
        try:
            if _is_draw_doc(ctx.doc):
                from plugin.draw.tables import delete_draw_table
                from plugin.framework.errors import make_tool_error

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
            nesting = _nesting_for(ctx.doc, name)
            _remove_writer_table(ctx.doc, table, name, nesting)
            return {
                "status": "ok",
                "message": "Table deleted",
                "table_name": name,
                "nesting": nesting,
            }
        except ValueError as ve:
            return self._tool_error(str(ve))
        except Exception as e:
            log.exception("Could not delete table '%s'", name)
            return self._tool_error("Could not delete table '%s': %s" % (name, e))
