# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""The 'tables' specialized domain: list/get/set cells + insert/delete + manage_table_structure.
Fakes implement the minimal XTextTable protocol; no LibreOffice required."""
from types import SimpleNamespace

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.writer.specialized.tables import (
    TableDelete,
    TableGetCells,
    TableInsert,
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
    def __init__(self, rows, cols, cells=None, name="T", anchor_text=None):
        self._rows = FakeBand(rows)
        self._cols = FakeBand(cols)
        self._cells = cells or {}
        self._name = name
        self._anchor_text = anchor_text

    def getName(self):
        return self._name

    def initialize(self, rows, cols):
        self._rows = FakeBand(rows)
        self._cols = FakeBand(cols)

    def getAnchor(self):
        if self._anchor_text is None:
            raise RuntimeError("no anchor")
        return SimpleNamespace(getText=lambda: self._anchor_text)

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

    def getCellByPosition(self, col, row):
        return self.getCellByName(_cell_name(col, row))


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

    def supportsService(self, name):
        return name == "com.sun.star.text.TextTable"

    def getSupportedServiceNames(self):
        return ("com.sun.star.text.TextTable",)

    def getName(self):
        return self._name


class FakeParagraph:
    def __init__(self, text="", portions=None):
        self._text = text
        self._portions = list(portions or [])

    def supportsService(self, name):
        return name == "com.sun.star.text.Paragraph"

    def getSupportedServiceNames(self):
        return ("com.sun.star.text.Paragraph",)

    def getString(self):
        return self._text

    def setString(self, value):
        self._text = value

    def createEnumeration(self):
        return FakeEnumeration(list(self._portions))


class FakePortion:
    def __init__(self, **props):
        self._props = props

    def getPropertyValue(self, key):
        if key not in self._props:
            raise RuntimeError(key)
        return self._props[key]


class FakeTextFrame:
    def __init__(self, elements):
        self._elements = list(elements)

    def supportsService(self, name):
        return name == "com.sun.star.text.TextFrame"

    def getSupportedServiceNames(self):
        return ("com.sun.star.text.TextFrame",)

    def getText(self):
        return SimpleNamespace(createEnumeration=lambda: FakeEnumeration(list(self._elements)))


class FakeParentTable(FakeTable):
    def __init__(self, rows, cols, cells=None, nested_by_cell=None, name="Parent"):
        super().__init__(rows, cols, cells=cells, name=name)
        self._nested_by_cell = nested_by_cell or {}
        self.cell_inserts = []
        self.cell_removes = []

    def getCellByName(self, name):
        cell = super().getCellByName(name)
        elements = self._nested_by_cell.get(name, [])
        cell.createEnumeration = lambda: FakeEnumeration(list(elements))
        cell.getEnd = lambda: "CELL_END"
        cell.getStart = lambda: "CELL_START"

        def insertTextContent(cursor, table, absorb):
            self.cell_inserts.append((name, cursor, table, absorb))
            nested_name = ""
            try:
                nested_name = str(table.getName() or "")
            except Exception:
                nested_name = ""
            if nested_name:
                self._nested_by_cell.setdefault(name, []).append(FakeTextTableElement(nested_name))

        def insertString(cursor, text, absorb):
            self._nested_by_cell.setdefault(name, []).insert(0, FakeParagraph(text))

        def removeTextContent(table):
            self.cell_removes.append((name, table))
            nested_name = ""
            try:
                nested_name = str(table.getName() or "")
            except Exception:
                nested_name = ""
            hosted = self._nested_by_cell.get(name, [])
            self._nested_by_cell[name] = [el for el in hosted if el.getName() != nested_name]

        cell.insertTextContent = insertTextContent
        cell.insertString = insertString
        cell.removeTextContent = removeTextContent
        return cell


class FakeBodyText:
    def __init__(self):
        self.inserts = []
        self.removed = []

    def getEnd(self):
        return "BODY_END"

    def insertTextContent(self, cursor, table, absorb):
        self.inserts.append((cursor, table, absorb))

    def removeTextContent(self, table):
        self.removed.append(table)


class FakeWriterDoc:
    """Minimal Writer doc for table_insert / table_delete unit tests."""

    def __init__(self, tables, *, body=None, view_cursor=None, view_error=None):
        self._tables_map = tables
        self._body = body or FakeBodyText()
        self._view_cursor = view_cursor
        self._view_error = view_error
        self._next = 1
        self.created = []

    def getTextTables(self):
        return FakeTables(self._tables_map)

    def getText(self):
        return self._body

    def createInstance(self, unused_service):
        name = "Table%d" % self._next
        self._next += 1
        table = FakeTable(1, 1, name=name)
        self.created.append(table)
        return table

    def getCurrentController(self):
        if self._view_error is not None:
            raise self._view_error
        return SimpleNamespace(getViewCursor=lambda: self._view_cursor)


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
    assert res["nested_in_cells"] == {}


def test_get_table_cells_reports_top_level_table_as_not_nested():
    standalone = FakeTable(1, 1, cells={"A1": "standalone-alpha"})
    res = TableGetCells().execute(_ctx({"Standalone": standalone}), name="Standalone")
    assert res["matrix"] == [["standalone-alpha"]]
    assert res["nesting"] == {
        "is_nested": False,
        "parent_table": None,
        "parent_cell": None,
    }
    assert res["nested_in_cells"] == {}


def test_list_tables_reports_nesting_and_nested_in_cells():
    child = FakeTable(1, 1)
    parent = FakeParentTable(2, 2, nested_by_cell={"B2": [FakeTextTableElement("Child")]})
    res = TableList().execute(_ctx({"Parent": parent, "Child": child}))
    assert res["status"] == "ok"
    by = {t["name"]: t for t in res["tables"]}
    assert by["Child"]["nesting"] == {
        "is_nested": True,
        "parent_table": "Parent",
        "parent_cell": "B2",
    }
    assert by["Child"]["nested_in_cells"] == {}
    assert by["Parent"]["nesting"] == {
        "is_nested": False,
        "parent_table": None,
        "parent_cell": None,
    }
    assert by["Parent"]["nested_in_cells"] == {"B2": ["Child"]}


def test_get_table_cells_parent_reports_nested_in_cells():
    child = FakeTable(1, 1)
    parent = FakeParentTable(2, 2, nested_by_cell={"B2": [FakeTextTableElement("Child")]})
    res = TableGetCells().execute(_ctx({"Parent": parent, "Child": child}), name="Parent")
    assert res["status"] == "ok"
    assert res["nesting"]["is_nested"] is False
    assert res["nested_in_cells"] == {"B2": ["Child"]}
    # Host slot is host paragraphs only — not the child's getString() dump.
    assert res["matrix"][1][1] == ""


def test_get_table_cells_host_matrix_is_paragraph_siblings_only():
    """getString() on a host cell concatenates inner-table text; matrix must not."""
    child = FakeTable(1, 1, cells={"A1": "INNER"})
    parent = FakeParentTable(
        2, 2,
        cells={"B2": "CAPTIONINNER"},
        nested_by_cell={"B2": [FakeParagraph("CAPTION"), FakeTextTableElement("Child")]},
    )
    res = TableGetCells().execute(_ctx({"Parent": parent, "Child": child}), name="Parent")
    assert res["status"] == "ok"
    assert res["matrix"][1][1] == "CAPTION"
    assert "INNER" not in res["matrix"][1][1]


def test_get_table_cells_reports_table_inside_frame_portion_as_nested():
    """As-character frames hang off paragraph portions, not cell XEnumeration siblings."""
    child = FakeTable(1, 1)
    frame = FakeTextFrame([FakeTextTableElement("Child")])
    para = FakeParagraph("", portions=[FakePortion(TextFrame=frame, TextContent=None)])
    parent = FakeParentTable(2, 2, nested_by_cell={"B2": [para]})
    res = TableGetCells().execute(_ctx({"Parent": parent, "Child": child}), name="Child")
    assert res["nesting"] == {
        "is_nested": True,
        "parent_table": "Parent",
        "parent_cell": "B2",
    }


def test_get_table_cells_reports_table_inside_frame_as_nested():
    child = FakeTable(1, 1)
    parent = FakeParentTable(
        2, 2,
        nested_by_cell={"B2": [FakeTextFrame([FakeTextTableElement("Child")])]},
    )
    res = TableGetCells().execute(_ctx({"Parent": parent, "Child": child}), name="Child")
    assert res["nesting"] == {
        "is_nested": True,
        "parent_table": "Parent",
        "parent_cell": "B2",
    }
    listed = TableList().execute(_ctx({"Parent": parent, "Child": child}))
    by = {t["name"]: t for t in listed["tables"]}
    assert by["Parent"]["nested_in_cells"] == {"B2": ["Child"]}


def test_get_table_cells_triple_nest_reports_direct_parent():
    """A grandchild reports the mid table, not the outer — no ancestry walk."""
    inner = FakeTable(1, 1, cells={"A1": "deep"})
    mid = FakeParentTable(1, 1, nested_by_cell={"A1": [FakeTextTableElement("Inner")]})
    outer = FakeParentTable(2, 2, nested_by_cell={"B2": [FakeTextTableElement("Mid")]})
    ctx = _ctx({"Outer": outer, "Mid": mid, "Inner": inner})
    inner_res = TableGetCells().execute(ctx, name="Inner")
    assert inner_res["nesting"] == {
        "is_nested": True,
        "parent_table": "Mid",
        "parent_cell": "A1",
    }
    mid_res = TableGetCells().execute(ctx, name="Mid")
    assert mid_res["nesting"] == {
        "is_nested": True,
        "parent_table": "Outer",
        "parent_cell": "B2",
    }
    assert mid_res["nested_in_cells"] == {"A1": ["Inner"]}
    outer_res = TableGetCells().execute(ctx, name="Outer")
    assert outer_res["nesting"]["is_nested"] is False
    assert outer_res["nested_in_cells"] == {"B2": ["Mid"]}


def test_get_table_cells_unknown_table_lists_names():
    res = TableGetCells().execute(_ctx({"Real": FakeTable(1, 1)}), name="Ghost")
    assert res["status"] == "error" and "Real" in res["message"]


# ---- set cell ---------------------------------------------------------------

def test_set_table_cell_ok():
    t = FakeTable(2, 2, cells={"B2": "old"})
    res = TableSetCell().execute(_ctx({"T": t}), name="T", cell="b2", text="new")
    assert res["status"] == "ok" and res["old_text"] == "old" and res["new_text"] == "new"
    assert t._cells["B2"] == "new"


def test_set_table_cell_host_keeps_nested_table():
    """Host cell: rewrite paragraphs, leave the nested TextTable (do not setString)."""
    parent = FakeParentTable(
        2, 2, cells={"A1": "keep", "B2": "host"},
        nested_by_cell={"B2": [FakeTextTableElement("Child")]},
    )
    child = FakeTable(1, 1)
    ctx = _ctx({"Parent": parent, "Child": child})
    res = TableSetCell().execute(ctx, name="Parent", cell="B2", text="caption")
    assert res["status"] == "ok", res
    assert res["nested_tables"] == ["Child"]
    assert isinstance(parent._nested_by_cell["B2"][0], FakeParagraph)
    assert parent._nested_by_cell["B2"][0].getString() == "caption"
    assert parent._nested_by_cell["B2"][-1].getName() == "Child"
    sibling = TableSetCell().execute(ctx, name="Parent", cell="A1", text="ok")
    assert sibling["status"] == "ok" and parent._cells["A1"] == "ok"


def test_set_table_cell_host_rewrites_existing_paragraphs():
    para = FakeParagraph("old-caption")
    child = FakeTable(1, 1, name="Child")
    parent = FakeParentTable(
        2, 2,
        nested_by_cell={"B2": [para, FakeTextTableElement("Child")]},
        name="Parent",
    )
    ctx = _ctx({"Parent": parent, "Child": child})
    res = TableSetCell().execute(ctx, name="Parent", cell="B2", text="new-caption")
    assert res["status"] == "ok", res
    assert res["old_text"] == "old-caption" and res["new_text"] == "new-caption"
    assert res["nested_tables"] == ["Child"]
    assert para.getString() == "new-caption"
    assert parent._nested_by_cell["B2"][-1].getName() == "Child"


def test_set_cell_keeps_nested_table_in_frame():
    parent = FakeParentTable(
        2, 2,
        cells={"B2": "host"},
        nested_by_cell={"B2": [FakeTextFrame([FakeTextTableElement("Child")])]},
    )
    res = TableSetCell().execute(
        _ctx({"Parent": parent, "Child": FakeTable(1, 1)}), name="Parent", cell="B2", text="caption"
    )
    assert res["status"] == "ok", res
    assert res["nested_tables"] == ["Child"]
    assert parent._nested_by_cell["B2"][-1].getName() == "Child" if hasattr(
        parent._nested_by_cell["B2"][-1], "getName"
    ) else True
    # Frame stays; a caption paragraph is inserted at the start.
    assert any(isinstance(el, FakeTextFrame) for el in parent._nested_by_cell["B2"])


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


def test_delete_row_refuses_nested_host():
    """removeByIndex on the host row would destroy the nested table."""
    parent = FakeParentTable(2, 2, nested_by_cell={"B2": [FakeTextTableElement("Child")]})
    child = FakeTable(1, 1)
    ctx = _ctx({"Parent": parent, "Child": child})
    tool = ManageTableStructure()
    res = tool.execute(ctx, action="delete", axis="row", name="Parent", index=1)
    assert res["status"] == "error" and "Child" in res["message"]
    assert parent._rows.n == 2
    # The other row has no nested table — delete still works.
    ok = tool.execute(ctx, action="delete", axis="row", name="Parent", index=0)
    assert ok["status"] == "ok" and parent._rows.n == 1


def test_delete_row_refuses_nested_host_in_frame():
    parent = FakeParentTable(
        2, 2, nested_by_cell={"B2": [FakeTextFrame([FakeTextTableElement("Child")])]}
    )
    res = ManageTableStructure().execute(
        _ctx({"Parent": parent, "Child": FakeTable(1, 1)}),
        action="delete", axis="row", name="Parent", index=1,
    )
    assert res["status"] == "error" and "Child" in res["message"]
    assert parent._rows.n == 2


def test_raise_if_range_hosts_nested_table_magicmock_selection_is_noop():
    """Auto-mocked UNO selections must not hang the HTML insert nested-table check."""
    from unittest.mock import MagicMock

    from plugin.writer.specialized.tables import raise_if_range_hosts_nested_table

    raise_if_range_hosts_nested_table(MagicMock())


def test_raise_if_range_hosts_nested_table_cell_vs_body():
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer.specialized.tables import raise_if_range_hosts_nested_table

    parent = FakeParentTable(2, 2, nested_by_cell={"B2": [FakeTextTableElement("Child")]})
    cell = parent.getCellByName("B2")
    cell.createTextCursorByRange = lambda start: SimpleNamespace(
        getPropertyValue=lambda key: object() if key == "TextTable" else None
    )
    rng = SimpleNamespace(getText=lambda: cell, getStart=lambda: "S")
    try:
        raise_if_range_hosts_nested_table(rng)
        raise AssertionError("expected ToolExecutionError")
    except ToolExecutionError as exc:
        assert "Child" in str(exc) and "table_set_cell" in str(exc)

    body = SimpleNamespace(
        createTextCursorByRange=lambda start: SimpleNamespace(getPropertyValue=lambda key: None),
        createEnumeration=lambda: FakeEnumeration([FakeTextTableElement("Top")]),
    )
    raise_if_range_hosts_nested_table(SimpleNamespace(getText=lambda: body, getStart=lambda: "S"))


def test_delete_column_refuses_nested_host():
    parent = FakeParentTable(2, 2, nested_by_cell={"B2": [FakeTextTableElement("Child")]})
    child = FakeTable(1, 1)
    ctx = _ctx({"Parent": parent, "Child": child})
    tool = ManageTableStructure()
    res = tool.execute(ctx, action="delete", axis="column", name="Parent", index=1)
    assert res["status"] == "error" and "Child" in res["message"]
    assert parent._cols.n == 2
    ok = tool.execute(ctx, action="delete", axis="column", name="Parent", index=0)
    assert ok["status"] == "ok" and parent._cols.n == 1


def test_insert_row_unaffected_by_nested_table():
    parent = FakeParentTable(2, 2, nested_by_cell={"B2": [FakeTextTableElement("Child")]})
    child = FakeTable(1, 1)
    res = ManageTableStructure().execute(
        _ctx({"Parent": parent, "Child": child}),
        action="insert", axis="row", name="Parent", index=2,
    )
    assert res["status"] == "ok" and res["rows"] == 3


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


# ---- insert / delete --------------------------------------------------------

def test_table_insert_nests_into_parent_cell():
    parent = FakeParentTable(2, 2, name="Parent")
    doc = FakeWriterDoc({"Parent": parent})
    res = TableInsert().execute(SimpleNamespace(doc=doc), rows=2, columns=2, parent="Parent", cell="B2")
    assert res["status"] == "ok"
    assert res["nesting"] == {"is_nested": True, "parent_table": "Parent", "parent_cell": "B2"}
    assert parent.cell_inserts
    cell_name, cursor, table, absorb = parent.cell_inserts[-1]
    assert cell_name == "B2" and cursor == "CELL_END" and absorb is False
    assert table.getRows().n == 2 and table.getColumns().n == 2
    assert doc._body.inserts == []


def test_table_insert_parent_xor_cell_errors():
    parent = FakeParentTable(2, 2, name="Parent")
    ctx = SimpleNamespace(doc=FakeWriterDoc({"Parent": parent}))
    tool = TableInsert()
    only_parent = tool.execute(ctx, rows=1, columns=1, parent="Parent")
    assert only_parent["status"] == "error" and "cell" in only_parent["message"]
    only_cell = tool.execute(ctx, rows=1, columns=1, cell="B2")
    assert only_cell["status"] == "error" and "parent" in only_cell["message"]


def test_table_insert_unknown_parent_or_cell():
    parent = FakeParentTable(2, 2, name="Parent")
    ctx = SimpleNamespace(doc=FakeWriterDoc({"Parent": parent}))
    tool = TableInsert()
    ghost = tool.execute(ctx, rows=1, columns=1, parent="Ghost", cell="A1")
    assert ghost["status"] == "error" and "Ghost" in ghost["message"]
    bad_cell = tool.execute(ctx, rows=1, columns=1, parent="Parent", cell="Z9")
    assert bad_cell["status"] == "error" and "Its cells are:" in bad_cell["message"]


def test_table_insert_body_uses_view_cursor_or_end():
    doc = FakeWriterDoc({}, view_cursor="VIEW")
    res = TableInsert().execute(SimpleNamespace(doc=doc), rows=1, columns=1)
    assert res["status"] == "ok"
    assert res["nesting"]["is_nested"] is False
    assert doc._body.inserts[-1][0] == "VIEW"
    doc_no_view = FakeWriterDoc({}, view_error=RuntimeError("no controller"))
    res_end = TableInsert().execute(SimpleNamespace(doc=doc_no_view), rows=1, columns=1)
    assert res_end["status"] == "ok"
    assert doc_no_view._body.inserts[-1][0] == "BODY_END"


def test_table_insert_wrong_start_node_tells_caller_to_pass_parent_cell():
    body = FakeBodyText()

    def boom(cursor, table, absorb):
        raise RuntimeError("End of content node doesn't have the proper start node")

    body.insertTextContent = boom
    doc = FakeWriterDoc({}, body=body, view_cursor="IN_CELL")
    res = TableInsert().execute(SimpleNamespace(doc=doc), rows=1, columns=1)
    assert res["status"] == "error"
    assert "parent" in res["message"] and "cell" in res["message"]


def test_table_delete_nested_via_anchor():
    host = FakeBodyText()
    child = FakeTable(1, 1, name="Child", anchor_text=host)
    parent = FakeParentTable(2, 2, nested_by_cell={"B2": [FakeTextTableElement("Child")]}, name="Parent")
    ctx = SimpleNamespace(doc=FakeWriterDoc({"Parent": parent, "Child": child}))
    res = TableDelete().execute(ctx, name="Child")
    assert res["status"] == "ok"
    assert res["nesting"] == {"is_nested": True, "parent_table": "Parent", "parent_cell": "B2"}
    assert host.removed == [child]


def test_table_delete_top_level_via_anchor():
    body = FakeBodyText()
    table = FakeTable(2, 2, name="T", anchor_text=body)
    ctx = SimpleNamespace(doc=FakeWriterDoc({"T": table}, body=body))
    res = TableDelete().execute(ctx, name="T")
    assert res["status"] == "ok"
    assert res["nesting"]["is_nested"] is False
    assert body.removed == [table]


def test_table_delete_nesting_fallback_when_anchor_fails():
    child = FakeTable(1, 1, name="Child")  # getAnchor raises
    parent = FakeParentTable(2, 2, nested_by_cell={"B2": [FakeTextTableElement("Child")]}, name="Parent")
    ctx = SimpleNamespace(doc=FakeWriterDoc({"Parent": parent, "Child": child}))
    res = TableDelete().execute(ctx, name="Child")
    assert res["status"] == "ok"
    assert parent.cell_removes and parent.cell_removes[-1][0] == "B2"
    assert parent.cell_removes[-1][1] is child


def test_table_delete_unknown_name_lists_open_tables():
    res = TableDelete().execute(SimpleNamespace(doc=FakeWriterDoc({"Real": FakeTable(1, 1, name="Real")})), name="Ghost")
    assert res["status"] == "error" and "Real" in res["message"]


def test_table_delete_requires_name():
    res = TableDelete().execute(SimpleNamespace(doc=FakeWriterDoc({})), name="")
    assert res["status"] == "error" and "name is required" in res["message"]


# ---- domain registration ----------------------------------------------------

def test_tables_are_specialized_domain():
    for cls in (
        TableList,
        TableGetCells,
        TableSetCell,
        ManageTableStructure,
        TableInsert,
        TableDelete,
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
