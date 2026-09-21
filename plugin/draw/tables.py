# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Draw/Impress TableShape helpers (not Writer XTextTable)."""

from __future__ import annotations

import re
from typing import Any

_A1 = re.compile(r"^([A-Za-z]+)(\d+)$")


def _table_model(shape):
    if hasattr(shape, "Model"):
        model = shape.Model
        if model is not None:
            return model
    if hasattr(shape, "Table"):
        return shape.Table
    try:
        return shape.getModel()
    except Exception:
        return None


def fill_table_cells(table, data) -> int:
    """Write a 2D string grid into ``table.getCellByPosition(col, row)``. Returns cells written."""
    written = 0
    for r_idx, row in enumerate(data):
        if not isinstance(row, (list, tuple)):
            continue
        for c_idx, val in enumerate(row):
            cell = table.getCellByPosition(c_idx, r_idx)
            text = "" if val is None else str(val)
            _set_cell_string(cell, text)
            written += 1
    return written


def _set_cell_string(cell, text: str) -> None:
    if hasattr(cell, "getText"):
        try:
            cell.getText().setString(text)
            return
        except Exception:
            pass
    if hasattr(cell, "setString"):
        cell.setString(text)


def _cell_string(cell) -> str:
    try:
        val = cell.getString()
        if val is not None:
            return str(val)
    except Exception:
        pass
    try:
        return str(cell.getText().getString() or "")
    except Exception:
        return ""


def _is_table_shape(shape) -> bool:
    try:
        st = shape.getShapeType()
        if st and "TableShape" in str(st):
            return True
    except Exception:
        pass
    try:
        return bool(shape.supportsService("com.sun.star.drawing.TableShape"))
    except Exception:
        return False


def _model_dims(model) -> tuple[int, int]:
    rows = model.getRows().getCount()
    cols = model.getColumns().getCount()
    return int(rows), int(cols)


def _ensure_table_dims(model, rows: int, columns: int) -> tuple[int, int]:
    """Grow a TableShape model to at least ``rows`` x ``columns``.

    TableShape defaults to 1x1 after ``page.add``. ``Rows``/``Columns``
    properties on a detached shape are often ignored, so insert uses the
    same ``insertByIndex`` path as ``manage_draw_structure``. Does not shrink.
    """
    nrows, ncols = _model_dims(model)
    band = model.getRows()
    while nrows < rows:
        band.insertByIndex(nrows, 1)
        nrows += 1
    band = model.getColumns()
    while ncols < columns:
        band.insertByIndex(ncols, 1)
        ncols += 1
    return nrows, ncols


def iter_table_shapes(doc) -> list[dict[str, Any]]:
    """List TableShapes as dicts: page, index, name, rows, cols, shape, model."""
    out: list[dict[str, Any]] = []
    pages = doc.getDrawPages()
    for page_i in range(pages.getCount()):
        page = pages.getByIndex(page_i)
        for shape_i in range(page.getCount()):
            shape = page.getByIndex(shape_i)
            if not _is_table_shape(shape):
                continue
            model = _table_model(shape)
            rows = cols = 0
            if model is not None:
                try:
                    rows, cols = _model_dims(model)
                except Exception:
                    pass
            name = ""
            try:
                name = str(shape.Name or "")
            except Exception:
                pass
            out.append(
                {
                    "page": page_i,
                    "index": shape_i,
                    "name": name,
                    "rows": rows,
                    "cols": cols,
                    "shape": shape,
                    "model": model,
                }
            )
    return out


def parse_a1(raw: str) -> tuple[int, int] | None:
    """Spreadsheet A1 → (col, row) 0-based. None if invalid."""
    m = _A1.match((raw or "").strip())
    if not m:
        return None
    letters, num = m.group(1).upper(), int(m.group(2))
    if num < 1:
        return None
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - ord("A") + 1)
    return col - 1, num - 1


def resolve_draw_table(doc, *, name: str = "", page=None, index=None) -> dict[str, Any]:
    tables = iter_table_shapes(doc)
    if not tables:
        raise ValueError("No tables on this Draw/Impress document.")
    name = (name or "").strip()
    if name:
        matches = [t for t in tables if t["name"] == name]
        if page is not None:
            matches = [t for t in matches if t["page"] == int(page)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            listing = ", ".join("%s(page %s idx %s)" % (t["name"] or "?", t["page"], t["index"]) for t in tables)
            raise ValueError("No table named '%s'. Open tables (call table_list): %s." % (name, listing))
        raise ValueError("Several tables named '%s'; pass page as well." % name)
    if page is not None and index is not None:
        for t in tables:
            if t["page"] == int(page) and t["index"] == int(index):
                return t
        raise ValueError("No table at page %s index %s." % (page, index))
    if len(tables) == 1:
        return tables[0]
    raise ValueError("Pass name or page+index (call table_list).")


def list_draw_tables(doc) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in iter_table_shapes(doc):
        rows = int(t["rows"] or 0)
        cols = int(t["cols"] or 0)
        out.append(
            {
                "name": t["name"],
                "rows": t["rows"],
                "cols": t["cols"],
                "cell_count": rows * cols,
                "page": t["page"],
                "index": t["index"],
            }
        )
    return out


def get_draw_cells(entry: dict[str, Any]) -> list[list[str]]:
    model = entry.get("model")
    if model is None:
        raise ValueError("Table cell model is unavailable.")
    rows, cols = _model_dims(model)
    matrix = []
    for r in range(rows):
        row = []
        for c in range(cols):
            try:
                row.append(_cell_string(model.getCellByPosition(c, r)))
            except Exception:
                row.append("")
        matrix.append(row)
    return matrix


def get_draw_cell(entry: dict[str, Any], cell_raw: str) -> str:
    """One Draw cell by spreadsheet A1 (``parse_a1``). Shared schema with Writer ``cell=``."""
    model = entry.get("model")
    if model is None:
        raise ValueError("Table cell model is unavailable.")
    parsed = parse_a1(cell_raw)
    if parsed is None:
        raise ValueError("Cell '%s' is not A1-style (e.g. B2)." % cell_raw)
    col, row = parsed
    nrows, ncols = _model_dims(model)
    if col < 0 or row < 0 or col >= ncols or row >= nrows:
        raise ValueError("Cell '%s' out of range (%s rows x %s cols)." % (cell_raw, nrows, ncols))
    return _cell_string(model.getCellByPosition(col, row))


def set_draw_cell(entry: dict[str, Any], cell_raw: str, text: str) -> tuple[str, str]:
    model = entry.get("model")
    if model is None:
        raise ValueError("Table cell model is unavailable.")
    parsed = parse_a1(cell_raw)
    if parsed is None:
        raise ValueError("Cell '%s' is not A1-style (e.g. B2)." % cell_raw)
    col, row = parsed
    nrows, ncols = _model_dims(model)
    if col < 0 or row < 0 or col >= ncols or row >= nrows:
        raise ValueError("Cell '%s' out of range (%s rows x %s cols)." % (cell_raw, nrows, ncols))
    cell = model.getCellByPosition(col, row)
    old = _cell_string(cell)
    _set_cell_string(cell, text)
    return old, text


def manage_draw_structure(entry: dict[str, Any], action: str, axis_arg: str, idx: int) -> tuple[int, int]:
    model = entry.get("model")
    if model is None:
        raise ValueError("Table cell model is unavailable.")
    axis = "rows" if axis_arg == "row" else "columns"
    band = model.getRows() if axis == "rows" else model.getColumns()
    count = band.getCount()
    if action == "insert":
        if idx > count:
            raise ValueError("index %d out of range (table has %d %s; use 0..%d)." % (idx, count, axis, count))
        band.insertByIndex(idx, 1)
    else:
        if idx >= count:
            raise ValueError("index %d out of range (table has %d %s; use 0..%d)." % (idx, count, axis, count - 1))
        if count <= 1:
            raise ValueError("Cannot remove the last %s of a table." % axis[:-1])
        band.removeByIndex(idx, 1)
    return _model_dims(model)


def insert_draw_table(ctx, **kwargs) -> dict[str, Any]:
    """Create a TableShape on a Draw/Impress page. Returns a tool-result dict."""
    from com.sun.star.awt import Point, Size
    from plugin.draw.bridge import DrawBridge
    from plugin.draw.layout import coerce_int

    rows = kwargs.get("rows")
    columns = kwargs.get("columns")
    if rows is None or columns is None:
        return {"status": "error", "message": "rows and columns are required.", "code": "TOOL_EXECUTION_ERROR"}
    rows = int(rows)
    columns = int(columns)
    if rows < 1 or columns < 1:
        return {"status": "error", "message": "rows and columns must be at least 1.", "code": "TOOL_EXECUTION_ERROR"}

    bridge = DrawBridge(ctx.doc)
    idx = kwargs.get("page")
    actual_idx = idx if idx is not None else ctx.active_page_index
    if actual_idx is None:
        actual_idx = bridge.get_active_page_index()
    try:
        page = bridge.get_pages().getByIndex(actual_idx)
    except Exception:
        return {"status": "error", "message": "Invalid page index: %s" % actual_idx, "code": "TOOL_EXECUTION_ERROR"}
    if page is None:
        return {"status": "error", "message": "No draw page available.", "code": "TOOL_EXECUTION_ERROR"}

    x = coerce_int(kwargs.get("x"), 3000)
    y = coerce_int(kwargs.get("y"), 4000)
    width = coerce_int(kwargs.get("width"), 20000)
    height = coerce_int(kwargs.get("height"), 10000)

    try:
        shape = ctx.doc.createInstance("com.sun.star.drawing.TableShape")
        for prop, val in (("Rows", rows), ("Columns", columns)):
            try:
                shape.setPropertyValue(prop, val)
            except Exception:
                pass
        page.add(shape)
        shape.setSize(Size(width, height))
        shape.setPosition(Point(x, y))
    except Exception as exc:
        return {"status": "error", "message": "Failed to create table: %s" % exc, "code": "TOOL_EXECUTION_ERROR"}

    written = 0
    data = kwargs.get("data")
    table = _table_model(shape)
    if table is None:
        if data:
            return {
                "status": "ok",
                "message": "Table inserted but cell model was unavailable; data not filled",
                "page": actual_idx,
                "index": page.getCount() - 1,
                "warning": "table_model_unavailable",
            }
    else:
        try:
            _ensure_table_dims(table, rows, columns)
        except Exception as exc:
            return {"status": "error", "message": "Failed to size table: %s" % exc, "code": "TOOL_EXECUTION_ERROR"}
        if data:
            try:
                written = fill_table_cells(table, data)
            except Exception as exc:
                return {
                    "status": "error",
                    "message": "Failed to fill table cells: %s" % exc,
                    "code": "TOOL_EXECUTION_ERROR",
                }

    return {
        "status": "ok",
        "message": "Table inserted",
        "page": actual_idx,
        "index": page.getCount() - 1,
        "rows": rows,
        "columns": columns,
        "cells_written": written,
    }


def delete_draw_table(doc, *, name: str = "", page=None, index=None) -> dict[str, Any]:
    """Remove a TableShape from its Draw/Impress page. Returns a tool-result dict."""
    entry = resolve_draw_table(doc, name=name, page=page, index=index)
    pages = doc.getDrawPages()
    page_obj = pages.getByIndex(int(entry["page"]))
    page_obj.remove(entry["shape"])
    return {
        "status": "ok",
        "message": "Table deleted",
        "table_name": entry.get("name") or name,
        "page": entry.get("page"),
        "index": entry.get("index"),
    }
