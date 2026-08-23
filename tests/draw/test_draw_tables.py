# WriterAgent — unit tests for Draw TableShape fill helper and table_insert
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock, patch

from plugin.draw.tables import fill_table_cells, insert_draw_table, parse_a1, _ensure_table_dims
from plugin.writer.specialized.tables import TableInsert


class _Cell:
    def __init__(self):
        self._s = ""

    def getText(self):
        return self

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
