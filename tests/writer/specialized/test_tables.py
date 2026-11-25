# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""The 'tables' specialized domain: list/get/set cells + insert/delete + table_manage_structure.
Fakes implement the minimal XTextTable protocol; no LibreOffice required."""
from types import SimpleNamespace

import pytest

from tests.harness.strip_bundle import requires_asserts


from plugin.writer.specialized.tables import (
    TableDelete,
    TableGetCells,
    TableInsert,
    TableList,
    ManageTableStructure,
    TableMergeCells,
    TableSetCell,
    TableSplitCell,
    _hosted_in_band,
    _unknown_cell_message,
    _writer_named_cells,
    record_table_deletions,
)
from plugin.writer.range_copy import (
    writer_cell_position,
    _writer_table_copy_layout,
)


def _col_letters(col_idx: int) -> str:
    """0-based column index -> spreadsheet letters (0->A, 25->Z, 26->AA)."""
    s = ""
    n = col_idx
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def _cell_name(col_idx: int, row_idx: int) -> str:
    """0-based (col, row) -> A1-style name (col 0/row 0 -> 'A1')."""
    return "%s%d" % (_col_letters(col_idx), row_idx + 1)


# 5×4 table after merge A1:D1: covered B1/C1/D1 drop; D2 remains (20 − 3 = 17).
_MERGED_BANNER_NAMES = ["A1"] + [
    "%s%d" % (col, row) for row in range(2, 6) for col in "ABCD"
]


class FakeBand:
    def __init__(self, n, seps_fn=None):
        self.n = n
        self.inserts = []
        self.removes = []
        self._seps_fn = seps_fn

    def getCount(self):
        return self.n

    def getByIndex(self, idx):
        positions = self._seps_fn(idx) if self._seps_fn is not None else []
        seps = [SimpleNamespace(Position=p) for p in positions]
        return SimpleNamespace(
            getPropertyValue=lambda prop: seps if prop == "TableColumnSeparators" else None,
        )

    def insertByIndex(self, idx, count):
        self.inserts.append((idx, count))
        self.n += count

    def removeByIndex(self, idx, count):
        self.removes.append((idx, count))
        self.n -= count


class FakeTable:
    def __init__(self, rows, cols, cells=None, name="T", anchor_text=None, cell_names=None, seps_by_row=None, row_spans=None, protected=None):
        self._cols = FakeBand(cols)
        self._cells = cells or {}
        self._name = name
        self._anchor_text = anchor_text
        # When set, do not invent a full rectangle — merged/covered names stay absent.
        self._explicit_names = cell_names
        self._seps_by_row = seps_by_row or {}
        self._row_spans = row_spans or {}
        self._protected = set(protected or ())
        self._rows = FakeBand(rows, seps_fn=self._row_separators)

    def _row_separators(self, row_idx):
        if row_idx in self._seps_by_row:
            return list(self._seps_by_row[row_idx])
        if self._explicit_names is not None:
            from plugin.writer.range_copy import writer_cell_position
            count = sum(
                1 for n in self._explicit_names
                if "." not in n and (pos := writer_cell_position(n)) is not None and pos[1] == row_idx
            )
            ncols = max(count, 1)
        else:
            ncols = self._cols.n
        return [10000 * c // ncols for c in range(1, ncols)]

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
        if self._explicit_names is not None:
            return list(self._explicit_names)
        return [
            "%s%d" % (chr(ord("A") + c), r + 1)
            for r in range(self._rows.n) for c in range(self._cols.n)
        ]

    def getCellByName(self, name):
        if self._explicit_names is not None and name not in self._explicit_names:
            raise RuntimeError("no cell %s" % name)
        return SimpleNamespace(
            getString=lambda: self._cells.get(name, ""),
            setString=lambda v: self._cells.__setitem__(name, v),
            getPropertyValue=lambda prop: (
                self._row_spans.get(name, 1)
                if prop == "RowSpan"
                else (name in self._protected if prop == "IsProtected" else None)
            ),
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
    def __init__(self, text="", portions=None, anchored=()):
        self._text = text
        self._portions = list(portions or [])
        self._anchored = list(anchored)

    def supportsService(self, name):
        return name == "com.sun.star.text.Paragraph"

    def getSupportedServiceNames(self):
        return ("com.sun.star.text.Paragraph",)

    def getString(self):
        return self._text

    def setString(self, value):
        self._text = value
        self._portions = []

    def createEnumeration(self):
        if self._portions:
            return FakeEnumeration(list(self._portions))
        if self._text:
            return FakeEnumeration([FakePortion(TextPortionType="Text", Text=self._text)])
        return FakeEnumeration([])

    def createContentEnumeration(self, service):
        return FakeEnumeration(list(self._anchored))


class FakePortion:
    def __init__(self, **props):
        self._props = props

    def getPropertyValue(self, key):
        if key not in self._props:
            raise RuntimeError(key)
        return self._props[key]

    def getString(self):
        return str(self._props.get("Text", "") or "")


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
    def __init__(self, rows, cols, cells=None, nested_by_cell=None, name="Parent", cell_names=None):
        super().__init__(rows, cols, cells=cells, name=name, cell_names=cell_names)
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


# ---- list / get -------------------------------------------------------------

def test_list_tables():
    res = TableList().execute(_ctx({"Table1": FakeTable(2, 3), "Fees": FakeTable(5, 2)}))
    assert res["status"] == "ok" and res["count"] == 2
    by = {t["name"]: (t["rows"], t["cols"], t["cell_count"]) for t in res["tables"]}
    assert by["Table1"] == (2, 3, 6) and by["Fees"] == (5, 2, 10)


def test_list_tables_cell_count_for_merged_layout():
    t = FakeTable(5, 1, cell_names=_MERGED_BANNER_NAMES)
    res = TableList().execute(_ctx({"T": t}))
    assert res["status"] == "ok"
    assert res["tables"][0]["rows"] == 5
    assert res["tables"][0]["cols"] == 1
    assert res["tables"][0]["cell_count"] == 17
    assert res["tables"][0]["cell_count"] != 5 * 1


def test_get_table_cells_writer_name_map_no_matrix():
    t = FakeTable(2, 2, cells={"A1": "x", "B1": "y", "A2": "z", "B2": "w"})
    res = TableGetCells().execute(_ctx({"T": t}), name="T")
    assert res["status"] == "ok"
    assert "matrix" not in res
    assert res["cells"] == {"A1": "x", "B1": "y", "A2": "z", "B2": "w"}
    assert res["cell_names"] == ["A1", "B1", "A2", "B2"]


def test_get_table_cells_merged_layout_has_d2_omits_b1_no_matrix():
    t = FakeTable(
        5,
        1,
        cells={"A1": "Title", "D2": "Telèfon responsable", "A2": ""},
        cell_names=_MERGED_BANNER_NAMES,
    )
    res = TableGetCells().execute(_ctx({"T": t}), name="T")
    assert res["status"] == "ok"
    assert "matrix" not in res
    assert res["rows"] == 5 and res["cols"] == 1
    assert "D2" in res["cell_names"]
    assert res["cells"]["D2"] == "Telèfon responsable"
    assert res["cells"]["A2"] == ""
    assert "B1" not in res["cells"]
    assert "B1" not in res["cell_names"]


def test_get_table_cells_optional_cell_one_address():
    t = FakeTable(
        5,
        1,
        cells={"A1": "Title", "D2": "Telèfon responsable"},
        cell_names=_MERGED_BANNER_NAMES,
    )
    res = TableGetCells().execute(_ctx({"T": t}), name="T", cell="D2")
    assert res["status"] == "ok"
    assert res["cell"] == "D2"
    assert res["cells"] == {"D2": "Telèfon responsable"}
    assert res["cell_names"] == ["D2"]
    assert "matrix" not in res
    missing = TableGetCells().execute(_ctx({"T": t}), name="T", cell="B1")
    assert missing["status"] == "error" and "Its cells are:" in missing["message"]
    assert "D2" in missing["message"]


def test_get_table_cells_reports_direct_nested_parent():
    child = FakeTable(1, 1, cells={"A1": "nested-alpha"})
    parent = FakeParentTable(
        2,
        2,
        nested_by_cell={"B2": [FakeTextTableElement("Child")]},
    )
    res = TableGetCells().execute(_ctx({"Parent": parent, "Child": child}), name="Child")
    assert res["cells"] == {"A1": "nested-alpha"}
    assert "matrix" not in res
    assert res["nesting"] == {
        "is_nested": True,
        "parent_table": "Parent",
        "parent_cell": "B2",
    }
    assert res["nested_in_cells"] == {}


def test_get_table_cells_reports_top_level_table_as_not_nested():
    standalone = FakeTable(1, 1, cells={"A1": "standalone-alpha"})
    res = TableGetCells().execute(_ctx({"Standalone": standalone}), name="Standalone")
    assert res["cells"] == {"A1": "standalone-alpha"}
    assert "matrix" not in res
    assert res["nesting"] == {
        "is_nested": False,
        "parent_table": None,
        "parent_cell": None,
    }
    assert res["nested_in_cells"] == {}


def test_nesting_top_level_table_does_not_walk_other_tables():
    body = FakeBodyText()
    target = FakeTable(2, 2, cells={"A1": "val"}, name="T", anchor_text=body)
    called = []

    def explode(name):
        called.append(name)
        raise AssertionError("unexpected cell scan")

    exploding = FakeTable(2, 2, name="Other")
    exploding.getCellByName = explode
    doc = SimpleNamespace(
        getTextTables=lambda: FakeTables({"T": target, "Other": exploding}),
        getText=lambda: body,
    )
    ctx = SimpleNamespace(doc=doc)
    res = TableGetCells().execute(ctx, name="T", cell="A1")
    assert res["status"] == "ok"
    assert res["cells"] == {"A1": "val"}
    assert res["nesting"]["is_nested"] is False
    assert called == []


def test_delete_top_level_table_does_not_walk_other_tables():
    body = FakeBodyText()
    target = FakeTable(2, 2, name="T", anchor_text=body)
    called = []

    def explode(name):
        called.append(name)
        raise AssertionError("unexpected cell scan")

    exploding = FakeTable(2, 2, name="Other")
    exploding.getCellByName = explode
    doc = FakeWriterDoc({"T": target, "Other": exploding}, body=body)
    ctx = SimpleNamespace(doc=doc)
    res = TableDelete().execute(ctx, name="T")
    assert res["status"] == "ok"
    assert res["nesting"]["is_nested"] is False
    assert body.removed == [target]
    assert called == []


def test_nesting_falls_back_to_scan_when_anchor_is_not_body():
    body = FakeBodyText()
    child = FakeTable(1, 1, cells={"A1": "nested-alpha"}, name="Child", anchor_text=SimpleNamespace())
    parent = FakeParentTable(
        2,
        2,
        nested_by_cell={"B2": [FakeTextTableElement("Child")]},
        name="Parent",
    )
    doc = SimpleNamespace(
        getTextTables=lambda: FakeTables({"Parent": parent, "Child": child}),
        getText=lambda: body,
    )
    res = TableGetCells().execute(SimpleNamespace(doc=doc), name="Child")
    assert res["cells"] == {"A1": "nested-alpha"}
    assert res["nesting"] == {
        "is_nested": True,
        "parent_table": "Parent",
        "parent_cell": "B2",
    }


def test_get_table_cells_optional_cell_reads_only_that_cell():
    body = FakeBodyText()
    target = FakeTable(3, 3, cells={"C3": "z"}, name="T", anchor_text=body)
    calls = []
    real_get_cell = target.getCellByName

    def tracking_get_cell(cell_name):
        calls.append(cell_name)
        return real_get_cell(cell_name)

    target.getCellByName = tracking_get_cell
    doc = SimpleNamespace(getTextTables=lambda: FakeTables({"T": target}), getText=lambda: body)
    res = TableGetCells().execute(SimpleNamespace(doc=doc), name="T", cell="C3")
    assert res["status"] == "ok"
    assert res["cells"] == {"C3": "z"}
    assert res["cell"] == "C3"
    assert calls == ["C3"]


def test_get_table_cells_optional_cell_on_host_cell():
    child = FakeTable(1, 1, cells={"A1": "INNER"}, name="Child")
    parent = FakeParentTable(
        2,
        2,
        cells={"B2": "CAPTIONINNER"},
        nested_by_cell={"B2": [FakeParagraph("CAPTION"), FakeTextTableElement("Child")]},
        name="Parent",
    )
    res = TableGetCells().execute(_ctx({"Parent": parent, "Child": child}), name="Parent", cell="B2")
    assert res["status"] == "ok"
    assert res["cells"] == {"B2": "CAPTION"}
    assert res["nested_in_cells"] == {"B2": ["Child"]}
    assert res["cell"] == "B2"


def test_get_table_cells_optional_cell_unreadable_is_error():
    table = FakeTable(2, 2, name="T")

    def fail_b1(name):
        if name == "B1":
            raise RuntimeError("disk error")
        return SimpleNamespace(getString=lambda: "")

    table.getCellByName = fail_b1
    res = TableGetCells().execute(_ctx({"T": table}), name="T", cell="B1")
    assert res["status"] == "error"
    assert "Could not read cell 'B1'" in res["message"]


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
    assert res["cells"]["B2"] == ""
    assert "matrix" not in res


def test_get_table_cells_host_text_is_paragraph_siblings_only():
    """getString() on a host cell concatenates inner-table text; cells must not."""
    child = FakeTable(1, 1, cells={"A1": "INNER"})
    parent = FakeParentTable(
        2, 2,
        cells={"B2": "CAPTIONINNER"},
        nested_by_cell={"B2": [FakeParagraph("CAPTION"), FakeTextTableElement("Child")]},
    )
    res = TableGetCells().execute(_ctx({"Parent": parent, "Child": child}), name="Parent")
    assert res["status"] == "ok"
    assert res["cells"]["B2"] == "CAPTION"
    assert "INNER" not in res["cells"]["B2"]
    assert "matrix" not in res


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
    # Frame stays; a caption paragraph is inserted at the start.
    assert any(isinstance(el, FakeTextFrame) for el in parent._nested_by_cell["B2"])


def test_set_table_cell_out_of_bounds_lists_real_names():
    res = TableSetCell().execute(_ctx({"T": FakeTable(2, 2)}), name="T", cell="Z9", text="x")
    assert res["status"] == "error" and "Its cells are:" in res["message"] and "A1" in res["message"]


def test_set_table_cell_uno_failure_returns_clean_error():
    """A non-ValueError UNO failure BEFORE cell resolution reaches execute_safe as itself."""
    def boom():
        raise RuntimeError("uno exploded")
    ctx = SimpleNamespace(doc=SimpleNamespace(getTextTables=boom))
    res = TableSetCell().execute_safe(ctx, name="T", cell="A1", text="x")
    assert res["status"] == "error" and "uno exploded" in res["message"]


def test_set_table_cell_never_blind_uppercases_real_lowercase_names():
    """Writer names columns A..Z then LOWERCASE a..z: on a wide table 'a1' and 'A1' are DIFFERENT
    cells. An exact lowercase name must be used as-is, never rewritten to uppercase."""
    t = FakeTable(1, 2, cells={"A1": "first", "a1": "col27"})
    # Simulate the wide-table naming: real names include both 'A1' and 'a1'.
    t.getCellNames = lambda: ["A1", "a1"]
    res = TableSetCell().execute(_ctx({"T": t}), name="T", cell="a1", text="new")
    assert res["status"] == "ok" and res["cell"] == "a1"
    assert t._cells["a1"] == "new" and t._cells["A1"] == "first"  # A1 untouched


def test_writer_cell_position_matches_get_cell_position():
    """A, Z, a, AA — Writer base 52, not spreadsheet parse_a1."""
    from plugin.draw.tables import parse_a1

    assert writer_cell_position("A1") == (0, 0)
    assert writer_cell_position("Z1") == (25, 0)
    assert writer_cell_position("a1") == (26, 0)
    assert writer_cell_position("AA1") == (52, 0)
    assert writer_cell_position("D2") == (3, 1)
    assert writer_cell_position("A10") == (0, 9)
    # o3tl::toInt32 stops at the first non-digit: split cell A1.1.1 is row 0.
    assert writer_cell_position("A1.1.1") == (0, 0)
    assert writer_cell_position("D2.1") == (3, 1)
    assert parse_a1("AA1") == (26, 0)
    assert parse_a1("a1") == (0, 0)
    assert writer_cell_position("") is None
    assert writer_cell_position("1A") is None


def test_unknown_cell_message_shared_sample():
    names = ["A1", "A2", "B2", "C2", "D2", "A3", "B3", "C3", "D3"]
    msg = _unknown_cell_message("B1", "T", names)
    assert "Its cells are:" in msg and "A1" in msg and "D3" in msg


def test_writer_table_copy_layout_keeps_d2():
    t = FakeTable(5, 1, cell_names=_MERGED_BANNER_NAMES)
    dest_rows, dest_cols, names = _writer_table_copy_layout(t)
    assert dest_rows == 5 and dest_cols == 4
    assert "D2" in names and "B1" not in names


def test_writer_table_copy_layout_ignores_split_suffix_for_size():
    """A1.1.1 is the same band as A1 — dest must not grow from the suffix."""
    t = FakeTable(5, 1, cell_names=_MERGED_BANNER_NAMES + ["A1.1.1"])
    dest_rows, dest_cols, names = _writer_table_copy_layout(t)
    assert dest_rows == 5 and dest_cols == 4
    assert "A1.1.1" in names


def test_copy_table_copies_merged_d2_by_name():
    """HTML copy walks getCellNames() so merged/irregular cells like D2 are not dropped."""
    from plugin.writer.range_copy import copy_table

    src = FakeTable(
        5,
        1,
        cells={"A1": "Title", "D2": "Telèfon responsable"},
        cell_names=_MERGED_BANNER_NAMES,
    )
    src.getName = lambda: "Table1"
    written = {}

    class DestCell:
        def __init__(self, name):
            self._name = name

        def setString(self, value):
            written[self._name] = value

        def createTextCursor(self):
            return SimpleNamespace(gotoStart=lambda unused_expand: None)

        def createEnumeration(self):
            raise RuntimeError("no enum")

        def getString(self):
            return written.get(self._name, "")

    class DestTable:
        def initialize(self, rows, cols):
            self.rows = rows
            self.cols = cols

        def setName(self, name):
            self.name = name

        def getCellByName(self, name):
            return DestCell(name)

        def getCellByPosition(self, col, row):
            raise AssertionError("position walk must not run when names exist")

    dest_table = DestTable()
    dest_text = FakeBodyText()
    dest_doc = SimpleNamespace(
        createInstance=lambda unused_service: dest_table,
        getText=lambda: dest_text,
        getTextTables=lambda: SimpleNamespace(hasByName=lambda _n: False),
    )
    copy_table(SimpleNamespace(), src, dest_doc, dest_text, SimpleNamespace(), None)
    assert dest_table.rows == 5 and dest_table.cols == 4
    assert dest_table.name == "Table1"
    assert written.get("D2") == "Telèfon responsable"
    assert "B1" not in written


def test_hosted_in_band_scans_named_cells_not_range_cols():
    """Banner row separators are [] so column 0 spans the whole table and hosts D2's nested table."""
    parent = FakeParentTable(
        5,
        1,
        cell_names=_MERGED_BANNER_NAMES,
        nested_by_cell={"D2": [FakeTextTableElement("Child")]},
    )
    assert _hosted_in_band(parent, "row", 1) == ["Child"]
    assert _hosted_in_band(parent, "row", 0) == []
    assert _hosted_in_band(parent, "column", 0) == ["Child"]


def test_hosted_in_band_sees_nest_in_split_cell_name():
    """Row band includes split cell A1.1.1; column band refuses split-cell tables."""
    parent = FakeParentTable(
        2,
        2,
        cell_names=["A1", "A1.1.1", "B1", "A2", "B2"],
        nested_by_cell={"A1.1.1": [FakeTextTableElement("Child")]},
    )
    assert _hosted_in_band(parent, "row", 0) == ["Child"]
    with pytest.raises(ValueError, match="split cells"):
        _hosted_in_band(parent, "column", 0)
    assert _hosted_in_band(parent, "row", 1) == []


def test_writer_named_cells_raises_on_a_failed_read() -> None:
    """A failed getCellNames raises immediately so callers fail closed."""
    from unittest.mock import MagicMock

    table = MagicMock()
    table.getCellNames.side_effect = RuntimeError("bridge")
    with pytest.raises(RuntimeError, match="bridge"):
        _writer_named_cells(table)
    with pytest.raises(RuntimeError, match="bridge"):
        _hosted_in_band(table, "row", 0)


def test_delete_last_column_guard():
    t = FakeTable(2, 1)
    res = ManageTableStructure().execute(
        _ctx({"T": t}), action="delete", axis="column", name="T", index=0
    )
    assert res["status"] == "error" and "last column" in res["message"]


@pytest.mark.parametrize(
    ("rows", "cols", "cell_names", "nested_factory", "check_other_row"),
    [
        # The other row has no nested table — delete still works.
        pytest.param(2, 2, None, lambda: {"B2": [FakeTextTableElement("Child")]}, True, id="direct_cell"),
        # cols=1 after A1:D1 merge; a nest in D2 must still refuse delete of that row.
        pytest.param(5, 1, _MERGED_BANNER_NAMES, lambda: {"D2": [FakeTextTableElement("Child")]}, False, id="merged_d2"),
        pytest.param(2, 2, None, lambda: {"B2": [FakeTextFrame([FakeTextTableElement("Child")])]}, False, id="in_frame"),
    ],
)
def test_delete_row_refuses_nested_host(rows, cols, cell_names, nested_factory, check_other_row):
    """removeByIndex on the host row would destroy the nested table."""
    parent = FakeParentTable(rows, cols, cell_names=cell_names, nested_by_cell=nested_factory())
    ctx = _ctx({"Parent": parent, "Child": FakeTable(1, 1)})
    tool = ManageTableStructure()
    res = tool.execute(ctx, action="delete", axis="row", name="Parent", index=1)
    assert res["status"] == "error" and "Child" in res["message"]
    assert parent._rows.n == rows
    if check_other_row:
        ok = tool.execute(ctx, action="delete", axis="row", name="Parent", index=0)
        assert ok["status"] == "ok" and parent._rows.n == rows - 1


def test_raise_if_range_hosts_nested_table_cell_vs_body():
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer.table_nesting import raise_if_range_hosts_nested_table

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
    tool = ManageTableStructure()
    ok, err = tool.validate(action="insert", axis="row", index="two")
    assert ok is False
    assert err is not None and "Invalid type for index" in err and "integer" in err


@requires_asserts
def test_manage_table_structure_non_integer_index_assertion():
    tool = ManageTableStructure()
    with pytest.raises(AssertionError):
        tool.execute(
            _ctx({"T": FakeTable(2, 2)}),
            action="insert",
            axis="row",
            name="T",
            index="two",
        )


@requires_asserts
def test_manage_table_structure_bad_action_or_axis():
    ctx = _ctx({"T": FakeTable(2, 2)})
    tool = ManageTableStructure()
    with pytest.raises(AssertionError):
        tool.execute(ctx, action="merge", axis="row", name="T", index=0)
    with pytest.raises(AssertionError):
        tool.execute(ctx, action="insert", axis="diagonal", name="T", index=0)




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

def test_table_insert_with_data_validates_dimensions():
    doc = FakeWriterDoc({})
    tool = TableInsert()
    # Too many rows in data
    res_rows = tool.execute(SimpleNamespace(doc=doc), rows=1, columns=2, data=[["A", "B"], ["C", "D"]])
    assert res_rows["status"] == "error" and "table only has 1" in res_rows["message"]
    # Too many columns in data
    res_cols = tool.execute(SimpleNamespace(doc=doc), rows=2, columns=1, data=[["A", "B"]])
    assert res_cols["status"] == "error" and "table only has 1" in res_cols["message"]


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
    res = TableInsert().execute(SimpleNamespace(doc=doc, selection_pin=None), rows=1, columns=1)
    assert res["status"] == "ok"
    assert res["nesting"]["is_nested"] is False
    assert doc._body.inserts[-1][0] == "VIEW"
    doc_no_view = FakeWriterDoc({}, view_error=RuntimeError("no controller"))
    res_end = TableInsert().execute(SimpleNamespace(doc=doc_no_view, selection_pin=None), rows=1, columns=1)
    assert res_end["status"] == "ok"
    assert doc_no_view._body.inserts[-1][0] == "BODY_END"


def test_table_insert_wrong_start_node_tells_caller_to_pass_parent_cell():
    body = FakeBodyText()

    def boom(cursor, table, absorb):
        raise RuntimeError("End of content node doesn't have the proper start node")

    body.insertTextContent = boom
    doc = FakeWriterDoc({}, body=body, view_cursor="IN_CELL")
    res = TableInsert().execute(SimpleNamespace(doc=doc, selection_pin=None), rows=1, columns=1)
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


# ---- table_delete under change tracking --------------------------------------
# With tracking on (review mode records every agent edit), table_delete
# records redlines rather than silently removing the table via removeTextContent.

class _TrackingCell:
    def __init__(self, table, cells, name):
        self._table, self._cells, self._name = table, cells, name

    def getString(self):
        return self._cells.get(self._name, "")

    def getPropertyValue(self, prop):
        if prop == "RowSpan":
            return self._table._row_spans.get(self._name, 1)
        if prop == "IsProtected":
            return self._name in self._table._protected
        return None

    def getEnd(self):
        return SimpleNamespace(_table=self._table, _cell_name=self._name)

    def createEnumeration(self):
        text = self.getString()
        if not text:
            return FakeEnumeration([])
        return FakeEnumeration([FakeParagraph(text)])

    def createTextCursor(self):
        cells, name = self._cells, self._name
        cursor = SimpleNamespace()
        cursor.gotoStart = lambda expand: None
        cursor.goRight = lambda count, expand: None
        cursor.setString = lambda v: cells.__setitem__(name, v)
        return cursor


class _TrackingTable(FakeTable):
    def getCellByName(self, name):
        return _TrackingCell(self, self._cells, name)


class _TrackingDoc(FakeWriterDoc):
    """RecordChanges on; .uno:DeleteTable behaves like Writer 26.2 (see tables.py)."""

    def __init__(self, tables, *, redlines_per_delete=1, keep_table=True, caret_lands=True):
        super().__init__(tables)
        self.redlines = 0
        self.dispatched = []
        self.selected = []
        self._redlines_per_delete = redlines_per_delete
        self._keep_table = keep_table
        self._caret_lands = caret_lands
        self._vc_table = None
        self._vc_cell_name = None
        self.previous_selection = object()

    def getPropertyValue(self, name):
        assert name == "RecordChanges"
        return True

    def getRedlines(self):
        items = [SimpleNamespace(getPropertyValue=lambda k: "Delete") for _ in range(self.redlines)]
        return SimpleNamespace(
            getCount=lambda: self.redlines,
            createEnumeration=lambda: FakeEnumeration(items),
        )

    def _select(self, target):
        self.selected.append(target)
        if self._caret_lands and hasattr(target, "_table") and hasattr(target, "_cell_name"):
            self._vc_table = target._table
            self._vc_cell_name = target._cell_name

    def _get_view_cursor(self):
        if self._vc_table is None or self._vc_cell_name is None:
            return SimpleNamespace(TextTable=None, Cell=None)
        return SimpleNamespace(
            TextTable=self._vc_table,
            Cell=SimpleNamespace(CellName=self._vc_cell_name),
        )

    def getCurrentController(self):
        return SimpleNamespace(
            getSelection=lambda: self.previous_selection,
            select=self._select,
            getViewCursor=self._get_view_cursor,
            getFrame=lambda: "frame",
        )

    def dispatch(self, command):
        self.dispatched.append(command)
        table = self._vc_table
        # Writer anchors each empty row with U+200D but records no redline for it.
        for name in table.getCellNames():
            if name.startswith("A") and not table._cells.get(name):
                table._cells[name] = "‍"
        self.redlines += self._redlines_per_delete
        if not self._keep_table:
            self._tables_map.pop(table.getName(), None)


def _uno_ctx_for(doc):
    helper = SimpleNamespace(executeDispatch=lambda frame, cmd, target, flags, args: doc.dispatch(cmd))
    smgr = SimpleNamespace(createInstanceWithContext=lambda name, ctx: helper)
    return SimpleNamespace(ServiceManager=smgr)


def test_tracked_delete_keeps_the_table_for_review():
    table = _TrackingTable(2, 2, cells={"A1": "Item", "A2": "Custas"}, name="T", anchor_text=FakeBodyText())
    doc = _TrackingDoc({"T": table})
    res = TableDelete().execute(SimpleNamespace(doc=doc, ctx=_uno_ctx_for(doc)), name="T")
    assert res["status"] == "ok" and res["pending_review"] is True
    assert doc.dispatched == [".uno:DeleteTable"]
    assert table._anchor_text.removed == [], "must not fall back to an untracked removeTextContent"
    assert doc.selected[-1] is doc.previous_selection, "the user's selection is restored"


def test_tracked_delete_marks_the_empty_row_anchor_too():
    """An empty row got only an unrecorded U+200D: accepting left that row behind."""
    table = _TrackingTable(3, 1, cells={"A1": "Item", "A3": "Custas"}, name="T")
    doc = _TrackingDoc({"T": table})
    TableDelete().execute(SimpleNamespace(doc=doc, ctx=_uno_ctx_for(doc)), name="T")
    assert table._cells["A2"] == "", "the anchor is deleted with tracking on, so its row has a redline"
    assert table._cells["A1"] == "Item", "cells that had text are left to the tracked delete"


def test_tracked_delete_fails_closed_when_nothing_was_recorded():
    table = _TrackingTable(1, 1, cells={"A1": "Item"}, name="T")
    doc = _TrackingDoc({"T": table}, redlines_per_delete=0)
    res = TableDelete().execute_safe(SimpleNamespace(doc=doc, ctx=_uno_ctx_for(doc)), name="T")
    assert res["status"] == "error" and "left in place" in res["message"]


def test_tracked_delete_reports_an_untracked_removal():
    table = _TrackingTable(1, 1, cells={"A1": "Item"}, name="T")
    doc = _TrackingDoc({"T": table}, redlines_per_delete=0, keep_table=False)
    res = TableDelete().execute_safe(SimpleNamespace(doc=doc, ctx=_uno_ctx_for(doc)), name="T")
    assert res["status"] == "error" and "no tracked change" in res["message"]


def test_tracked_delete_restores_selection_when_dispatch_fails():
    def fail_dispatch(name, ctx):
        raise RuntimeError("no dispatch")

    failing_uno_ctx = SimpleNamespace(
        ServiceManager=SimpleNamespace(createInstanceWithContext=fail_dispatch)
    )
    table = _TrackingTable(2, 2, cells={"A1": "Item"}, name="T", anchor_text=FakeBodyText())
    doc = _TrackingDoc({"T": table})
    res = TableDelete().execute_safe(SimpleNamespace(doc=doc, ctx=failing_uno_ctx), name="T")
    assert res["status"] == "error"
    assert "no dispatch" in res["message"]
    assert doc.selected[-1] is doc.previous_selection


def test_tracked_delete_fails_closed_when_anchor_cleanup_records_nothing():
    table = _TrackingTable(2, 1, cells={"A1": "Item"}, name="T")
    doc = _TrackingDoc({"T": table}, redlines_per_delete=0)
    res = TableDelete().execute_safe(SimpleNamespace(doc=doc, ctx=_uno_ctx_for(doc)), name="T")
    assert res["status"] == "error"
    assert "left in place" in res["message"]


def test_tracked_delete_caret_did_not_land_refuses_without_dispatch():
    table = _TrackingTable(2, 2, cells={"A1": "Item", "A2": "Other"}, name="T")
    doc = _TrackingDoc({"T": table}, caret_lands=False)
    res_table = TableDelete().execute_safe(SimpleNamespace(doc=doc, ctx=_uno_ctx_for(doc)), name="T")
    assert res_table["status"] == "error"
    assert res_table["code"] == "TABLE_DELETE_NOT_TRACKABLE"
    assert doc.dispatched == []

    res_row = ManageTableStructure().execute_safe(
        SimpleNamespace(doc=doc, ctx=_uno_ctx_for(doc)), action="delete", axis="row", name="T", index=0
    )
    assert res_row["status"] == "error"
    assert res_row["code"] == "TABLE_DELETE_NOT_TRACKABLE"
    assert doc.dispatched == []


def test_record_table_deletions_accepts_a_bare_context():
    session = SimpleNamespace(record_mutation=lambda fn: fn())
    ctx = SimpleNamespace()
    body = FakeBodyText()
    table = FakeTable(2, 2, name="T", anchor_text=body)
    doc = FakeWriterDoc({"T": table}, body=body)
    res = record_table_deletions(session, doc, ctx, [(table, "T")])
    assert res is False
    assert body.removed == [table]


def test_record_table_deletions_precomputes_nesting():
    body = FakeBodyText()
    child = FakeTable(1, 1, name="Child")  # getAnchor raises
    parent = FakeParentTable(
        2, 2, nested_by_cell={"B2": [FakeTextTableElement("Child")]}, name="Parent"
    )
    orig_remove = body.removeTextContent

    def body_remove(t):
        orig_remove(t)
        if t is parent:
            parent._nested_by_cell.clear()

    body.removeTextContent = body_remove

    doc = FakeWriterDoc({"Parent": parent, "Child": child}, body=body)
    session = SimpleNamespace(record_mutation=lambda fn: fn())
    ctx = SimpleNamespace()
    res = record_table_deletions(session, doc, ctx, [(parent, "Parent"), (child, "Child")])
    assert res is False
    assert (
        parent.cell_removes
        and parent.cell_removes[-1][0] == "B2"
        and parent.cell_removes[-1][1] is child
    )
    assert child not in body.removed


def test_cell_matrix_text_hides_tracked_deletions():
    """#43: a pending "velha" -> "nova" edit read "celula novavelha" (getString keeps deleted text)."""
    from unittest.mock import patch

    from plugin.writer.specialized import tables

    cell = SimpleNamespace(getString=lambda: "celula novavelha")
    t = FakeTable(1, 1)
    t.getCellByName = lambda name: cell
    with patch.object(tables, "cell_hosted_table_names", return_value=[]), \
         patch.object(tables, "get_string_without_tracked_deletions", return_value="celula nova"):
        res = tables.TableGetCells().execute(_ctx({"T": t}), name="T")
        assert res["status"] == "ok"
        assert res["cells"]["A1"] == "celula nova"


def test_empty_replacement_keeps_a_table_whose_cell_holds_a_pending_deletion():
    """Visible text "nova" with "velha" pending deletion is not an empty shell: deleting "nova"
    must edit the text, not delete the table (review finding on the #43 change)."""
    from unittest.mock import patch

    from plugin.writer import apply_document_content

    cell = SimpleNamespace(getString=lambda: "celula novavelha")
    table = SimpleNamespace(getCellNames=lambda: ["A1"], getCellByName=lambda name: cell)
    found = SimpleNamespace(getString=lambda: "celula nova")
    with patch.object(apply_document_content, "cell_hosted_table_names", return_value=[]), \
         patch.object(apply_document_content, "_range_in_cell", return_value=True):
        assert apply_document_content._empty_replacement_clears_table(table, [found]) is False


def test_table_insert_rejects_non_integer_dims_and_non_list_data():
    tool = TableInsert()
    ok, err = tool.validate(rows=[1], columns=2)
    assert ok is False
    assert err is not None and "Invalid type for rows" in err and "integer" in err
    res = tool.execute_safe(SimpleNamespace(doc=FakeWriterDoc({})), rows=[1], columns=2)
    assert res["status"] == "error"
    ok_data, err_data = tool.validate(rows=2, columns=2, data="ab")
    assert ok_data is False
    assert err_data is not None and "Invalid type for data" in err_data and "array" in err_data


@requires_asserts
def test_table_insert_non_list_data_assertion():
    tool = TableInsert()
    with pytest.raises(AssertionError):
        tool.execute(SimpleNamespace(doc=FakeWriterDoc({})), rows=2, columns=2, data="ab")


def test_table_set_cell_non_string_cell():
    tool = TableSetCell()
    ok, err = tool.validate(name="T", cell=123, text="new")
    assert ok is False
    assert err is not None and "cell" in err



def test_table_insert_draw_non_numeric_rows_columns():
    """Bug 2: TableInsert schema rejects non-integer rows/columns, and Draw table dims < 1 are rejected."""
    from unittest.mock import MagicMock
    from plugin.draw.tables import insert_draw_table
    from plugin.framework.errors import ToolExecutionError

    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s in ("com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument")
    ctx = SimpleNamespace(doc=doc, active_page_index=0)

    tool = TableInsert()
    ok, err = tool.validate(rows="invalid", columns=2)
    assert ok is False
    assert err and "integer" in err

    with pytest.raises(ToolExecutionError) as exc_info:
        insert_draw_table(ctx, rows=0, columns=2)
    assert "at least 1" in exc_info.value.message
    assert exc_info.value.code == "TOOL_EXECUTION_ERROR"

    res_exec = tool.execute(ctx, rows=0, columns=2)
    assert res_exec["status"] == "error"
    assert "at least 1" in res_exec["message"]
    assert res_exec["code"] == "TOOL_EXECUTION_ERROR"


class DisposedException(Exception):
    pass


def test_table_get_cells_cell_read_error_skipped_and_disposal_reraised():
    """Bug 3: TableGetCells skips failing cell on read failure, and re-raises disposal."""
    from unittest.mock import MagicMock

    table = FakeTable(1, 2, cells={"A1": "ok"})
    table._explicit_names = ["A1", "B1"]
    orig_get_cell = table.getCellByName

    def mock_get_cell(name):
        if name == "B1":
            raise RuntimeError("cell read error")
        return orig_get_cell(name)

    table.getCellByName = mock_get_cell

    doc = FakeWriterDoc({"T": table})
    ctx = SimpleNamespace(doc=doc)

    res = TableGetCells().execute(ctx, name="T")
    assert res["status"] == "ok"
    assert "A1" in res["cells"]
    assert "B1" not in res["cells"]

    disposed_table = FakeTable(1, 1)
    disposed_table.getCellByName = MagicMock(side_effect=DisposedException("object disposed"))
    with pytest.raises(DisposedException):
        TableGetCells().execute(SimpleNamespace(doc=FakeWriterDoc({"T": disposed_table})), name="T")



def test_table_list_skips_bad_table_and_reraises_disposal():
    """Bug 4: TableList skips corrupt table that fails _dims, and re-raises disposal."""
    from unittest.mock import MagicMock

    good = FakeTable(2, 2)
    bad = MagicMock()
    bad.getRows.side_effect = RuntimeError("corrupted table rows")

    doc = FakeWriterDoc({"Good": good, "Bad": bad})
    ctx = SimpleNamespace(doc=doc)

    res = TableList().execute(ctx)
    assert res["status"] == "ok"
    assert res["count"] == 1
    assert res["tables"][0]["name"] == "Good"

    disposed_table = MagicMock()
    disposed_table.getRows.side_effect = DisposedException("disposed document")
    doc_disposed = FakeWriterDoc({"Disposed": disposed_table})
    with pytest.raises(DisposedException):
        TableList().execute(SimpleNamespace(doc=doc_disposed))


def test_resolve_uno_context():
    from plugin.framework.uno_context import resolve_uno_context
    ctx = SimpleNamespace(ctx="CUSTOM_UNO_CTX")
    assert resolve_uno_context(ctx) == "CUSTOM_UNO_CTX"


def test_read_helpers_reraise_disposal():
    from unittest.mock import MagicMock
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.table_nesting import _is_text_table, cell_hosted_table_names

    disposed_obj = MagicMock()
    disposed_obj.supportsService.side_effect = DisposedException("disposed")
    with pytest.raises(DocumentDisposedError):
        _is_text_table(disposed_obj)

    disposed_cell = MagicMock()
    disposed_cell.createEnumeration.side_effect = DisposedException("disposed")
    with pytest.raises(DocumentDisposedError):
        cell_hosted_table_names(disposed_cell)


@pytest.mark.parametrize("cell_range", ["A1", "A1:B2:C3", ":B2", "A1:A1"])
def test_merge_cells_rejects_malformed_range(cell_range):
    table = FakeTable(2, 2, name="T")
    ctx = SimpleNamespace(doc=FakeWriterDoc({"T": table}))
    res = TableMergeCells().execute(ctx, name="T", cell_range=cell_range)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert table.getRows().getCount() == 2


def test_merge_cells_unknown_corner_lists_cells():
    table = FakeTable(2, 2, name="T")
    ctx = SimpleNamespace(doc=FakeWriterDoc({"T": table}))
    res = TableMergeCells().execute(ctx, name="T", cell_range="A1:Z9")
    assert res["status"] == "error"
    assert "Its cells are" in res["message"]


@pytest.mark.parametrize("parts", [1, 21])
def test_split_cell_parts_out_of_range(parts):
    table = FakeTable(2, 2, name="T")
    ctx = SimpleNamespace(doc=FakeWriterDoc({"T": table}))
    res = TableSplitCell().execute(ctx, name="T", cell="A1", direction="rows", parts=parts)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"


def test_merge_cells_reports_lo_refusal():
    class RefusingCursorTable(FakeTable):
        def createCursorByCellName(self, unused_cell_name):
            return SimpleNamespace(
                gotoCellByName=lambda unused_last, unused_expand: True,
                mergeRange=lambda: False,
            )

    table = RefusingCursorTable(2, 2, name="LegacyTbl")
    ctx = SimpleNamespace(doc=FakeWriterDoc({"LegacyTbl": table}))
    res = TableMergeCells().execute(ctx, name="LegacyTbl", cell_range="A1:B1")
    assert res["status"] == "error"
    assert "LegacyTbl" in res["message"]


@pytest.mark.parametrize(
    "cell_names,seps_by_row,idx,expected",
    [
        pytest.param(
            ["A1", "B1", "A2", "B2", "C2"],
            {0: [6667], 1: [3333, 6667]},
            0,
            {"A1", "A2", "B2"},
            id="first_row_merge_A1_B1",
        ),
        pytest.param(
            ["A1", "B1", "A2", "B2", "C2"],
            {0: [3333], 1: [3333, 6667]},
            1,
            {"B1", "B2", "C2"},
            id="first_row_merge_B1_C1",
        ),
        pytest.param(
            ["A1", "B1", "C1", "A2", "B2"],
            {0: [3333, 6667], 1: [3333]},
            1,
            {"B1", "C1", "B2"},
            id="later_row_merge_B2_C2",
        ),
    ],
)
def test_column_band_cells_merged_rows(cell_names, seps_by_row, idx, expected):
    from plugin.writer.range_copy import writer_cell_position
    from plugin.writer.specialized.tables import _column_band_cells

    table = FakeTable(2, 3, cell_names=cell_names, seps_by_row=seps_by_row)
    positions = {n: pos for n in cell_names if (pos := writer_cell_position(n)) is not None}
    assert set(_column_band_cells(table, positions, idx)) == expected


@pytest.mark.parametrize("tall_cell", ["A1", "B1"])
def test_tracked_row_delete_refuses_tall_cell_without_dispatch(tall_cell):
    table = _TrackingTable(
        3,
        3,
        cells={"A1": "A", "B1": "B", "C1": "C"},
        name="T",
        row_spans={tall_cell: 2},
    )
    doc = _TrackingDoc({"T": table})
    res = ManageTableStructure().execute_safe(
        SimpleNamespace(doc=doc, ctx=_uno_ctx_for(doc)), action="delete", axis="row", name="T", index=0
    )
    assert res["status"] == "error"
    assert res["code"] == "ROW_HAS_SPANNING_CELL"
    assert tall_cell in res["message"]
    assert doc.dispatched == []


@pytest.mark.parametrize("tall_cell", ["A1", "B1", "C1"])
def test_untracked_row_delete_refuses_tall_cell(tall_cell):
    table = FakeTable(3, 3, cells={tall_cell: "x"}, name="T", row_spans={tall_cell: 2})
    ctx = _ctx({"T": table})
    res = ManageTableStructure().execute(ctx, action="delete", axis="row", name="T", index=0)
    assert res["status"] == "error"
    assert res["code"] == "ROW_HAS_SPANNING_CELL"
    assert table._rows.removes == []


@pytest.mark.parametrize("tall_cell", ["A1", "B1", "C1"])
def test_untracked_row_delete_allows_empty_tall_cell(tall_cell):
    table = FakeParentTable(3, 3, name="T", nested_by_cell={tall_cell: [FakeParagraph("")]})
    table._row_spans = {tall_cell: 2}
    ctx = _ctx({"T": table})
    res = ManageTableStructure().execute(ctx, action="delete", axis="row", name="T", index=0)
    assert res["status"] == "ok", res
    assert table._rows.removes == [(0, 1)]


@pytest.mark.parametrize(
    ("para", "expected_keyword"),
    [
        pytest.param(FakeParagraph("", portions=[FakePortion(TextPortionType="Frame")]), "frame", id="frame_portion"),
        pytest.param(FakeParagraph("", portions=[FakePortion(TextPortionType="TextField")]), "field", id="field_portion"),
        pytest.param(FakeParagraph("", portions=[FakePortion(TextPortionType="Annotation")]), "comment", id="annotation_portion"),
        pytest.param(FakeParagraph("", portions=[FakePortion(TextPortionType="Bookmark")]), "bookmark", id="bookmark_portion"),
        pytest.param(FakeParagraph("", anchored=[object()]), "frame", id="anchored_content"),
    ],
)
def test_row_delete_refuses_tall_cell_with_hosted_content(para, expected_keyword):
    table = FakeParentTable(3, 3, name="T", nested_by_cell={"B1": [para]})
    table._row_spans = {"B1": 2}
    ctx = _ctx({"T": table})
    res = ManageTableStructure().execute(ctx, action="delete", axis="row", name="T", index=0)
    assert res["status"] == "error"
    assert res["code"] == "ROW_HAS_SPANNING_CELL"
    assert expected_keyword in res["message"]
    assert table._rows.removes == []


@pytest.mark.parametrize("axis", ["row", "column"])
@pytest.mark.parametrize("tracked", [False, True])
def test_delete_refuses_protected_cell(axis, tracked):
    if tracked:
        table = _TrackingTable(3, 3, name="T", protected={"B2"})
        doc = _TrackingDoc({"T": table})
        ctx = SimpleNamespace(doc=doc, ctx=_uno_ctx_for(doc))
    else:
        table = FakeTable(3, 3, name="T", protected={"B2"})
        doc = None
        ctx = _ctx({"T": table})
    res = ManageTableStructure().execute_safe(ctx, action="delete", axis=axis, name="T", index=1)
    assert res["status"] == "error"
    assert res["code"] == "CELLS_PROTECTED"
    assert "B2" in res["message"]
    band = table._rows if axis == "row" else table._cols
    assert band.removes == []
    if doc is not None:
        assert doc.dispatched == []


def test_untracked_delete_band_not_deleted():
    table = FakeTable(3, 3, name="T")
    table._rows.removeByIndex = lambda idx, count: None
    res = ManageTableStructure().execute_safe(_ctx({"T": table}), action="delete", axis="row", name="T", index=1)
    assert res["status"] == "error"
    assert res["code"] == "TABLE_BAND_NOT_DELETED"


@pytest.mark.parametrize("tall_cell", ["A1", "C1"])
def test_tracked_row_delete_refuses_empty_edge_tall_cell(tall_cell):
    cells = {name: "x" for name in ("A1", "B1", "C1") if name != tall_cell}
    table = _TrackingTable(3, 3, cells=cells, name="T", row_spans={tall_cell: 2})
    doc = _TrackingDoc({"T": table})
    res = ManageTableStructure().execute_safe(
        SimpleNamespace(doc=doc, ctx=_uno_ctx_for(doc)), action="delete", axis="row", name="T", index=0
    )
    assert res["status"] == "error"
    assert res["code"] == "ROW_HAS_SPANNING_CELL"
    assert tall_cell in res["message"]
    assert doc.dispatched == []


def test_tracked_row_delete_allows_empty_inner_tall_cell():
    table = _TrackingTable(
        3,
        3,
        cells={"A1": "a", "C1": "c"},
        name="T",
        row_spans={"B1": 2},
    )
    doc = _TrackingDoc({"T": table})
    res = ManageTableStructure().execute_safe(
        SimpleNamespace(doc=doc, ctx=_uno_ctx_for(doc)), action="delete", axis="row", name="T", index=0
    )
    assert res["status"] == "ok", res
    assert res["tracked"] is True
    assert doc.dispatched == [".uno:DeleteRows"]


def test_untracked_row_delete_covered_row_still_removes():
    table = FakeTable(
        3,
        3,
        name="T",
        cell_names=["A1", "B1", "C1", "B2", "C2", "A3", "B3", "C3"],
        row_spans={"A1": 2},
    )
    ctx = _ctx({"T": table})
    res = ManageTableStructure().execute(ctx, action="delete", axis="row", name="T", index=1)
    assert res["status"] == "ok"
    assert table._rows.removes == [(1, 1)]


@pytest.mark.parametrize(
    ("seps_per_row", "cell_names", "idx", "expected"),
    [
        # Header merge A1:B1 in 3-col table, deleting col 0 -> A1 spans 2 cols
        ([[6666], [3333, 6666], [3333, 6666]], ["A1", "B1", "A2", "B2", "C2", "A3", "B3", "C3"], 0, ("A1", 2, 2)),
        # Lower merge A3:B3 in 3-col table, deleting col 0 -> A3 spans 2 cols
        ([[3333, 6666], [3333, 6666], [6666]], ["A1", "B1", "C1", "A2", "B2", "C2", "A3", "B3"], 0, ("A3", 2, 2)),
        # Full-width row A3:C3 in 3-col table, deleting col 1 -> A3 spans 3 cols
        ([[3333, 6666], [3333, 6666], []], ["A1", "B1", "C1", "A2", "B2", "C2", "A3"], 1, ("A3", 3, 3)),
        # Rectangular 3x3 table -> None
        ([[3333, 6666], [3333, 6666], [3333, 6666]], ["A1", "B1", "C1", "A2", "B2", "C2", "A3", "B3", "C3"], 0, None),
        # A1:B1 merged in 2-col table, deleting idx 1 (beyond row 0's 1-cell count) -> None
        ([[], [5000]], ["A1", "A2", "B2"], 1, None),
        # Staggered B1:C1 and C2:D2 in 5-col table, deleting col 1 -> B1 spans 2, 3 cols deleted
        (
            [[2000, 6000, 8000], [2000, 4000, 8000], [2000, 4000, 6000, 8000]],
            ["A1", "B1", "C1", "D1", "A2", "B2", "C2", "D2", "A3", "B3", "C3", "D3", "E3"],
            1,
            ("B1", 2, 3),
        ),
        # A1:B1 and B2:C2 in 4-col table, deleting col 1 -> B2 is the spanning cell (2 cols)
        (
            [[5000, 7500], [2500, 7500], [2500, 5000, 7500]],
            ["A1", "B1", "C1", "A2", "B2", "C2", "A3", "B3", "C3", "D3"],
            1,
            ("B2", 2, 2),
        ),
    ],
)
def test_column_wide_cell(seps_per_row, cell_names, idx, expected):
    from unittest.mock import MagicMock
    from plugin.writer.specialized.tables import _column_wide_cell

    table = MagicMock()
    table.getCellNames.return_value = cell_names
    rows = MagicMock()
    rows.getCount.return_value = len(seps_per_row)
    row_objs = []
    for seps in seps_per_row:
        r = MagicMock()
        r.getPropertyValue.return_value = [SimpleNamespace(Position=p) for p in seps]
        row_objs.append(r)
    rows.getByIndex.side_effect = lambda i: row_objs[i]
    table.getRows.return_value = rows
    assert _column_wide_cell(table, idx) == expected


def test_pending_insertions_detects_inserted_table_spans():
    from plugin.writer.specialized.tables import _pending_insertions

    class _Node:
        def __init__(self, name, is_table):
            self._name = name
            self._is_table = is_table

        def supportsService(self, svc):
            return self._is_table and svc == "com.sun.star.text.TextTable"

        def getName(self):
            return self._name

    def _redline(rtype, start, end):
        props = {"RedlineType": rtype, "RedlineStart": start, "RedlineEnd": end}
        return SimpleNamespace(getPropertyValue=lambda k: props[k])

    def _doc_with(redlines):
        def _enum():
            items = list(redlines)
            return SimpleNamespace(
                hasMoreElements=lambda: bool(items),
                nextElement=lambda: items.pop(0),
            )

        return SimpleNamespace(
            getRedlines=lambda: SimpleNamespace(
                getCount=lambda: len(redlines),
                createEnumeration=_enum,
            )
        )

    names = ["T1", "T2", "T3"]
    t2 = _Node("T2", True)
    t3 = _Node("T3", True)
    body = _Node("body", False)
    cell = _Node("A1", False)

    assert _pending_insertions(_doc_with([_redline("Insert", t2, body)]), names) == {"T2", "T3"}
    assert _pending_insertions(_doc_with([_redline("Insert", t2, t3)]), names) == {"T2"}
    assert _pending_insertions(_doc_with([_redline("Insert", cell, body)]), names) == set()
    assert _pending_insertions(_doc_with([_redline("Delete", t2, body)]), names) == set()



