# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""The 'tables' specialized domain: list/get/set cells + manage_table_structure.
Fakes implement the minimal XTextTable protocol; no LibreOffice required."""
from types import SimpleNamespace

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.writer.specialized.tables import (
    TableGetCells,
    TableList,
    ManageTableStructure,
    TableSetCell,
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


class FakeEnumeration:
    def __init__(self, elements):
        self._elements = list(elements)
        self._index = 0

    def hasMoreElements(self):
        return self._index < len(self._elements)

    def nextElement(self):
        element = self._elements[self._index]
        self._index += 1
        return element


class FakeTextTableElement:
    def __init__(self, name):
        self._name = name

    def getSupportedServiceNames(self):
        return ("com.sun.star.text.TextTable",)

    def getName(self):
        return self._name


class FakeParentTable(FakeTable):
    def __init__(self, rows, cols, cells=None, nested_by_cell=None):
        super().__init__(rows, cols, cells=cells)
        self._nested_by_cell = nested_by_cell or {}

    def getCellByName(self, name):
        cell = super().getCellByName(name)
        elements = self._nested_by_cell.get(name, [])
        cell.createEnumeration = lambda: FakeEnumeration(elements)
        return cell


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
    res = TableList().execute(_ctx({"Table1": FakeTable(2, 3), "Fees": FakeTable(5, 2)}))
    assert res["status"] == "ok" and res["count"] == 2
    by = {t["name"]: (t["rows"], t["cols"]) for t in res["tables"]}
    assert by["Table1"] == (2, 3) and by["Fees"] == (5, 2)


def test_get_table_cells_matrix():
    t = FakeTable(2, 2, cells={"A1": "x", "B1": "y", "A2": "z", "B2": "w"})
    res = TableGetCells().execute(_ctx({"T": t}), name="T")
    assert res["matrix"] == [["x", "y"], ["z", "w"]]


def test_get_table_cells_reports_direct_nested_parent():
    child = FakeTable(1, 1, cells={"A1": "nested-alpha"})
    parent = FakeParentTable(
        2,
        2,
        nested_by_cell={"B2": [FakeTextTableElement("Child")]},
    )
    res = TableGetCells().execute(_ctx({"Parent": parent, "Child": child}), name="Child")
    assert res["matrix"] == [["nested-alpha"]]
    assert res["nesting"] == {
        "is_nested": True,
        "parent_table": "Parent",
        "parent_cell": "B2",
    }


def test_get_table_cells_reports_top_level_table_as_not_nested():
    standalone = FakeTable(1, 1, cells={"A1": "standalone-alpha"})
    res = TableGetCells().execute(_ctx({"Standalone": standalone}), name="Standalone")
    assert res["matrix"] == [["standalone-alpha"]]
    assert res["nesting"] == {
        "is_nested": False,
        "parent_table": None,
        "parent_cell": None,
    }


def test_get_table_cells_unknown_table_lists_names():
    res = TableGetCells().execute(_ctx({"Real": FakeTable(1, 1)}), name="Ghost")
    assert res["status"] == "error" and "Real" in res["message"]


# ---- set cell ---------------------------------------------------------------

def test_set_table_cell_ok():
    t = FakeTable(2, 2, cells={"B2": "old"})
    res = TableSetCell().execute(_ctx({"T": t}), name="T", cell="b2", text="new")
    assert res["status"] == "ok" and res["old_text"] == "old" and res["new_text"] == "new"
    assert t._cells["B2"] == "new"


def test_set_table_cell_out_of_bounds_lists_real_names():
    res = TableSetCell().execute(_ctx({"T": FakeTable(2, 2)}), name="T", cell="Z9", text="x")
    assert res["status"] == "error" and "Its cells are:" in res["message"] and "A1" in res["message"]


def test_set_table_cell_uno_failure_returns_clean_error():
    """A non-ValueError UNO failure BEFORE cell resolution must return a clean tool error, not
    an UnboundLocalError from the except block referencing an unassigned cell_name."""
    def boom():
        raise RuntimeError("uno exploded")
    ctx = SimpleNamespace(doc=SimpleNamespace(getTextTables=boom))
    res = TableSetCell().execute(ctx, name="T", cell="A1", text="x")
    assert res["status"] == "error" and "A1" in res["message"] and "uno exploded" in res["message"]


def test_set_table_cell_never_blind_uppercases_real_lowercase_names():
    """Writer names columns A..Z then LOWERCASE a..z: on a wide table 'a1' and 'A1' are DIFFERENT
    cells. An exact lowercase name must be used as-is, never rewritten to uppercase."""
    t = FakeTable(1, 2, cells={"A1": "first", "a1": "col27"})
    # Simulate the wide-table naming: real names include both 'A1' and 'a1'.
    t.getCellNames = lambda: ["A1", "a1"]
    res = TableSetCell().execute(_ctx({"T": t}), name="T", cell="a1", text="new")
    assert res["status"] == "ok" and res["cell"] == "a1"
    assert t._cells["a1"] == "new" and t._cells["A1"] == "first"  # A1 untouched


def test_get_table_cells_prefers_position_access():
    """Position-based reads are naming-scheme-proof; the computed-name fallback only runs when
    getCellByPosition is unavailable."""
    t = FakeTable(1, 1, cells={"A1": "by-name"})
    t.getCellByPosition = lambda c, r: SimpleNamespace(getString=lambda: "by-position")
    res = TableGetCells().execute(_ctx({"T": t}), name="T")
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
    res = TableGetCells().execute(_ctx({"T": t}), name="T")
    assert res["matrix"] == [["x", ""]]


def test_delete_last_column_guard():
    t = FakeTable(2, 1)
    res = ManageTableStructure().execute(
        _ctx({"T": t}), action="delete", axis="column", name="T", index=0
    )
    assert res["status"] == "error" and "last column" in res["message"]


# ---- rows / columns ---------------------------------------------------------

def test_insert_row_appends_and_within_bounds():
    t = FakeTable(2, 2)
    ctx = _ctx({"T": t})
    tool = ManageTableStructure()
    res = tool.execute(ctx, action="insert", axis="row", name="T", index=2)
    assert res["status"] == "ok" and res["rows"] == 3 and res["cols"] == 2
    assert t._rows.inserts[-1] == (2, 1)
    assert tool.execute(ctx, action="insert", axis="row", name="T", index=99)["status"] == "error"


def test_delete_row_bounds_and_last_row_guard():
    t = FakeTable(2, 2)
    ctx = _ctx({"T": t})
    tool = ManageTableStructure()
    res = tool.execute(ctx, action="delete", axis="row", name="T", index=1)
    assert res["status"] == "ok" and res["rows"] == 1 and res["cols"] == 2
    assert t._rows.removes[-1] == (1, 1)
    # now 1 row left -> deleting it is refused
    res = tool.execute(ctx, action="delete", axis="row", name="T", index=0)
    assert res["status"] == "error" and "last row" in res["message"]


def test_delete_row_out_of_range():
    res = ManageTableStructure().execute(
        _ctx({"T": FakeTable(3, 2)}), action="delete", axis="row", name="T", index=5
    )
    assert res["status"] == "error"


def test_column_ops():
    t = FakeTable(2, 3)
    ctx = _ctx({"T": t})
    tool = ManageTableStructure()
    res = tool.execute(ctx, action="insert", axis="column", name="T", index=1)
    assert res["status"] == "ok" and res["rows"] == 2 and res["cols"] == 4
    assert t._cols.inserts[-1] == (1, 1)
    res = tool.execute(ctx, action="delete", axis="column", name="T", index=0)
    assert res["status"] == "ok" and res["rows"] == 2 and res["cols"] == 3
    assert t._cols.removes[-1] == (0, 1)


def test_negative_index_errors():
    ctx = _ctx({"T": FakeTable(2, 2)})
    tool = ManageTableStructure()
    res = tool.execute(ctx, action="insert", axis="row", name="T", index=-1)
    assert res["status"] == "error" and "non-negative" in res["message"]
    res = tool.execute(ctx, action="delete", axis="column", name="T", index=-2)
    assert res["status"] == "error" and "non-negative" in res["message"]


def test_non_integer_index_errors():
    res = ManageTableStructure().execute(
        _ctx({"T": FakeTable(2, 2)}), action="insert", axis="row", name="T", index="two"
    )
    assert res["status"] == "error" and "integer" in res["message"]


def test_manage_table_structure_bad_action_or_axis():
    ctx = _ctx({"T": FakeTable(2, 2)})
    tool = ManageTableStructure()
    assert tool.execute(ctx, action="merge", axis="row", name="T", index=0)["status"] == "error"
    assert tool.execute(ctx, action="insert", axis="diagonal", name="T", index=0)["status"] == "error"


# ---- domain registration ----------------------------------------------------

def test_tables_are_specialized_domain():
    for cls in (
        TableList,
        TableGetCells,
        TableSetCell,
        ManageTableStructure,
    ):
        assert cls.tier == "specialized"
        assert cls.specialized_domain == "tables"


def test_table_tools_shortened_name_param():
    t = FakeTable(2, 2, cells={"A1": "val"})
    res_get = TableGetCells().execute(_ctx({"T": t}), name="T")
    assert res_get["status"] == "ok"
    assert res_get["matrix"][0][0] == "val"

    res_set = TableSetCell().execute(_ctx({"T": t}), name="T", cell="A1", text="new_val")
    assert res_set["status"] == "ok"

    res_struct = ManageTableStructure().execute(_ctx({"T": t}), action="insert", axis="row", name="T", index=0)
    assert res_struct["status"] == "ok"
