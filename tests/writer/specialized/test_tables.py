# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""The 'tables' specialized domain: list/get/set cells + insert/delete rows and columns.
Fakes implement the minimal XTextTable protocol; no LibreOffice required."""
from types import SimpleNamespace

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.writer.specialized.tables import (
    DeleteTableColumn,
    DeleteTableRow,
    GetTableCells,
    InsertTableColumn,
    InsertTableRow,
    ListTables,
    SetTableCell,
    _cell_name,
    _col_letters,
)


class FakeBand:
    def __init__(self, n):
        self.n = n
        self.inserts = []
        self.removes = []

    def getCount(self):
        return self.n

    def insertByIndex(self, idx, count):
        self.inserts.append((idx, count))
        self.n += count

    def removeByIndex(self, idx, count):
        self.removes.append((idx, count))
        self.n -= count


class FakeTable:
    def __init__(self, rows, cols, cells=None):
        self._rows = FakeBand(rows)
        self._cols = FakeBand(cols)
        self._cells = cells or {}

    def getRows(self):
        return self._rows

    def getColumns(self):
        return self._cols

    def getCellNames(self):
        return [
            "%s%d" % (chr(ord("A") + c), r + 1)
            for r in range(self._rows.n) for c in range(self._cols.n)
        ]

    def getCellByName(self, name):
        return SimpleNamespace(
            getString=lambda: self._cells.get(name, ""),
            setString=lambda v: self._cells.__setitem__(name, v),
        )


class FakeTables:
    def __init__(self, mapping):
        self._m = mapping

    def getElementNames(self):
        return list(self._m.keys())

    def hasByName(self, n):
        return n in self._m

    def getByName(self, n):
        return self._m[n]


def _ctx(tables):
    doc = SimpleNamespace(getTextTables=lambda: FakeTables(tables))
    return SimpleNamespace(doc=doc)


# ---- helpers ----------------------------------------------------------------

def test_cell_name_math():
    assert _col_letters(0) == "A" and _col_letters(25) == "Z" and _col_letters(26) == "AA"
    assert _cell_name(0, 0) == "A1" and _cell_name(1, 1) == "B2"


# ---- list / get -------------------------------------------------------------

def test_list_tables():
    res = ListTables().execute(_ctx({"Table1": FakeTable(2, 3), "Fees": FakeTable(5, 2)}))
    assert res["status"] == "ok" and res["count"] == 2
    by = {t["name"]: (t["rows"], t["cols"]) for t in res["tables"]}
    assert by["Table1"] == (2, 3) and by["Fees"] == (5, 2)


def test_get_table_cells_matrix():
    t = FakeTable(2, 2, cells={"A1": "x", "B1": "y", "A2": "z", "B2": "w"})
    res = GetTableCells().execute(_ctx({"T": t}), table_name="T")
    assert res["matrix"] == [["x", "y"], ["z", "w"]]


def test_get_table_cells_unknown_table_lists_names():
    res = GetTableCells().execute(_ctx({"Real": FakeTable(1, 1)}), table_name="Ghost")
    assert res["status"] == "error" and "Real" in res["message"]


# ---- set cell ---------------------------------------------------------------

def test_set_table_cell_ok():
    t = FakeTable(2, 2, cells={"B2": "old"})
    res = SetTableCell().execute(_ctx({"T": t}), table_name="T", cell="b2", text="new")
    assert res["status"] == "ok" and res["old_text"] == "old" and res["new_text"] == "new"
    assert t._cells["B2"] == "new"


def test_set_table_cell_out_of_bounds_lists_real_names():
    res = SetTableCell().execute(_ctx({"T": FakeTable(2, 2)}), table_name="T", cell="Z9", text="x")
    assert res["status"] == "error" and "Its cells are:" in res["message"] and "A1" in res["message"]


def test_set_table_cell_never_blind_uppercases_real_lowercase_names():
    """Writer names columns A..Z then LOWERCASE a..z: on a wide table 'a1' and 'A1' are DIFFERENT
    cells. An exact lowercase name must be used as-is, never rewritten to uppercase."""
    t = FakeTable(1, 2, cells={"A1": "first", "a1": "col27"})
    # Simulate the wide-table naming: real names include both 'A1' and 'a1'.
    t.getCellNames = lambda: ["A1", "a1"]
    res = SetTableCell().execute(_ctx({"T": t}), table_name="T", cell="a1", text="new")
    assert res["status"] == "ok" and res["cell"] == "a1"
    assert t._cells["a1"] == "new" and t._cells["A1"] == "first"  # A1 untouched


def test_get_table_cells_prefers_position_access():
    """Position-based reads are naming-scheme-proof; the computed-name fallback only runs when
    getCellByPosition is unavailable."""
    t = FakeTable(1, 1, cells={"A1": "by-name"})
    t.getCellByPosition = lambda c, r: SimpleNamespace(getString=lambda: "by-position")
    res = GetTableCells().execute(_ctx({"T": t}), table_name="T")
    assert res["matrix"] == [["by-position"]]


def test_get_table_cells_covered_cell_blank():
    """A merged/covered cell has no addressable name: both access paths fail -> ''."""
    t = FakeTable(1, 2, cells={"A1": "x"})
    real_get = t.getCellByName

    def get_by_name(name):
        if name == "B1":
            raise RuntimeError("covered cell")
        return real_get(name)

    t.getCellByName = get_by_name
    res = GetTableCells().execute(_ctx({"T": t}), table_name="T")
    assert res["matrix"] == [["x", ""]]


def test_delete_last_column_guard():
    t = FakeTable(2, 1)
    res = DeleteTableColumn().execute(_ctx({"T": t}), table_name="T", col_index=0)
    assert res["status"] == "error" and "last column" in res["message"]


# ---- rows / columns ---------------------------------------------------------

def test_insert_row_appends_and_within_bounds():
    t = FakeTable(2, 2)
    ctx = _ctx({"T": t})
    assert InsertTableRow().execute(ctx, table_name="T", row_index=2)["status"] == "ok"  # append
    assert t._rows.inserts[-1] == (2, 1)
    assert InsertTableRow().execute(ctx, table_name="T", row_index=99)["status"] == "error"  # out of range


def test_delete_row_bounds_and_last_row_guard():
    t = FakeTable(2, 2)
    ctx = _ctx({"T": t})
    assert DeleteTableRow().execute(ctx, table_name="T", row_index=1)["status"] == "ok"
    assert t._rows.removes[-1] == (1, 1)
    # now 1 row left -> deleting it is refused
    res = DeleteTableRow().execute(ctx, table_name="T", row_index=0)
    assert res["status"] == "error" and "last row" in res["message"]


def test_delete_row_out_of_range():
    res = DeleteTableRow().execute(_ctx({"T": FakeTable(3, 2)}), table_name="T", row_index=5)
    assert res["status"] == "error"


def test_column_ops():
    t = FakeTable(2, 3)
    ctx = _ctx({"T": t})
    assert InsertTableColumn().execute(ctx, table_name="T", col_index=1)["status"] == "ok"
    assert t._cols.inserts[-1] == (1, 1)
    assert DeleteTableColumn().execute(ctx, table_name="T", col_index=0)["status"] == "ok"
    assert t._cols.removes[-1] == (0, 1)


def test_non_integer_index_errors():
    res = InsertTableRow().execute(_ctx({"T": FakeTable(2, 2)}), table_name="T", row_index="two")
    assert res["status"] == "error" and "integer" in res["message"]


# ---- domain registration ----------------------------------------------------

def test_tables_are_specialized_domain():
    for cls in (ListTables, SetTableCell, InsertTableRow):
        assert cls.tier == "specialized"
        assert cls.specialized_domain == "tables"
