# WriterAgent — unit tests for Draw TableShape fill helper and table_insert
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock, patch

from plugin.draw.tables import (
    delete_draw_table,
    fill_table_cells,
    get_draw_cell,
    insert_draw_table,
    list_draw_tables,
    parse_a1,
    _ensure_table_dims,
)
from plugin.writer.specialized.tables import TableDelete, TableGetCells, TableInsert


class _Cell:
    def __init__(self):
        self._s = ""

    def getText(self):
        return self

    def getString(self):
        return self._s

    def setString(self, val):
        self._s = val


class _Band:
    def __init__(self, count):
        self._count = count

    def getCount(self):
        return self._count

    def insertByIndex(self, idx, n):
        self._count += n


class _Table:
    def __init__(self, rows=1, cols=1):
        self.cells = {}
        self._rows = _Band(rows)
        self._cols = _Band(cols)

    def getRows(self):
        return self._rows

    def getColumns(self):
        return self._cols

    def getCellByPosition(self, col, row):
        if col < 0 or col >= self._cols.getCount() or row < 0 or row >= self._rows.getCount():
            raise IndexError("col %s out of range 0..%s" % (col, self._cols.getCount()))
        key = (col, row)
        if key not in self.cells:
            self.cells[key] = _Cell()
        return self.cells[key]


def test_ensure_table_dims_grows_from_1x1():
    table = _Table(1, 1)
    assert _ensure_table_dims(table, 2, 2) == (2, 2)
    n = fill_table_cells(table, [["a", "b"], ["c", "d"]])
    assert n == 4
    assert table.cells[(1, 1)]._s == "d"


def test_fill_table_cells():
    table = _Table(2, 2)
    n = fill_table_cells(table, [["a", "b"], ["c", "d"]])
    assert n == 4
    assert table.cells[(0, 0)]._s == "a"
    assert table.cells[(1, 1)]._s == "d"


def test_list_draw_tables_includes_cell_count():
    with patch(
        "plugin.draw.tables.iter_table_shapes",
        return_value=[
            {"name": "T", "rows": 2, "cols": 3, "page": 0, "index": 1, "shape": None, "model": None}
        ],
    ):
        out = list_draw_tables(object())
    assert out == [{"name": "T", "rows": 2, "cols": 3, "cell_count": 6, "page": 0, "index": 1}]


def test_get_draw_cell_parse_a1():
    table = _Table(2, 3)
    table.getCellByPosition(1, 1)._s = "hello"
    assert get_draw_cell({"model": table, "rows": 2, "cols": 3}, "B2") == "hello"


def test_table_get_cells_draw_honors_cell():
    ctx = MagicMock()
    entry = {"name": "Fees", "page": 0, "index": 1, "rows": 2, "cols": 3, "model": object()}
    with patch("plugin.writer.specialized.tables._is_draw_doc", return_value=True):
        with patch("plugin.draw.tables.resolve_draw_table", return_value=entry):
            with patch("plugin.draw.tables.get_draw_cell", return_value="hello") as get_one:
                out = TableGetCells().execute(ctx, name="Fees", cell="B2")
    assert out["status"] == "ok"
    assert out["matrix"] == [["hello"]]
    assert out["cell"] == "B2"
    get_one.assert_called_once()


def test_parse_a1_valid():
    # Single-letter columns
    assert parse_a1("A1") == (0, 0)
    assert parse_a1("B2") == (1, 1)
    assert parse_a1("Z10") == (25, 9)

    # Multi-letter columns
    assert parse_a1("AA1") == (26, 0)
    assert parse_a1("AB5") == (27, 4)
    assert parse_a1("ZZ1") == (701, 0)
    assert parse_a1("AAA1") == (702, 0)

    # Lowercase & mixed-case inputs
    assert parse_a1("a1") == (0, 0)
    assert parse_a1("b2") == (1, 1)
    assert parse_a1("aA1") == (26, 0)

    # Surrounding whitespace
    assert parse_a1("  A1  ") == (0, 0)
    assert parse_a1("\tB2\n") == (1, 1)


def test_parse_a1_invalid():
    # None, empty, whitespace
    assert parse_a1(None) is None
    assert parse_a1("") is None
    assert parse_a1("   ") is None

    # Invalid row numbers
    assert parse_a1("A0") is None
    assert parse_a1("A-1") is None

    # Invalid formats
    assert parse_a1("1A") is None
    assert parse_a1("ABC") is None
    assert parse_a1("123") is None
    assert parse_a1("A 1") is None
    assert parse_a1("A$1") is None
    assert parse_a1("A1B") is None
    assert parse_a1("not") is None


def test_insert_table_ok():
    ctx = MagicMock()
    shape = MagicMock()
    page = MagicMock()
    page.getCount.return_value = 1
    ctx.doc.createInstance.return_value = shape
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_active_page_index.return_value = 0
        bridge.get_pages.return_value.getByIndex.return_value = page
        with patch("plugin.draw.tables._table_model", return_value=_Table(1, 1)):
            with patch("com.sun.star.awt.Point", MagicMock(), create=True), patch(
                "com.sun.star.awt.Size", MagicMock(), create=True
            ):
                out = insert_draw_table(ctx, rows=2, columns=2, data=[["h1", "h2"], ["v1", "v2"]])
    assert out["status"] == "ok"
    assert out["cells_written"] == 4
    page.add.assert_called_once_with(shape)


def test_insert_table_rejects_zero_rows():
    out = insert_draw_table(MagicMock(), rows=0, columns=2)
    assert out["status"] == "error"


def test_table_insert_tool_dispatches_draw():
    ctx = MagicMock()
    with patch("plugin.writer.specialized.tables._is_draw_doc", return_value=True):
        with patch("plugin.draw.tables.insert_draw_table", return_value={"status": "ok", "rows": 2}) as ins:
            out = TableInsert().execute(ctx, rows=2, columns=2)
    assert out["status"] == "ok"
    ins.assert_called_once()


def test_table_insert_draw_rejects_parent_cell():
    ctx = MagicMock()
    with patch("plugin.writer.specialized.tables._is_draw_doc", return_value=True):
        out = TableInsert().execute(ctx, rows=2, columns=2, parent="Outer", cell="A1")
    assert out["status"] == "error" and "Writer-only" in out["message"]


def test_delete_draw_table_removes_shape():
    page = MagicMock()
    pages = MagicMock()
    pages.getByIndex.return_value = page
    doc = MagicMock()
    doc.getDrawPages.return_value = pages
    shape = MagicMock()
    entry = {"page": 0, "index": 2, "name": "Fees", "shape": shape}
    with patch("plugin.draw.tables.resolve_draw_table", return_value=entry):
        out = delete_draw_table(doc, name="Fees")
    assert out["status"] == "ok"
    assert out["table_name"] == "Fees"
    page.remove.assert_called_once_with(shape)


def test_table_delete_tool_dispatches_draw():
    ctx = MagicMock()
    with patch("plugin.writer.specialized.tables._is_draw_doc", return_value=True):
        with patch(
            "plugin.draw.tables.delete_draw_table",
            return_value={"status": "ok", "table_name": "Fees"},
        ) as dele:
            out = TableDelete().execute(ctx, name="Fees")
    assert out["status"] == "ok"
    dele.assert_called_once()
