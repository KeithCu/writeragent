# WriterAgent — unit tests for Draw TableShape fill helper and table_insert
# SPDX-License-Identifier: GPL-3.0-or-later

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from plugin.draw.tables import (
    delete_draw_table,
    fill_table_cells,
    get_draw_cell,
    get_draw_cells,
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
        self.calls = []

    def getCount(self):
        return self._count

    def insertByIndex(self, idx, n):
        self.calls.append((idx, n))
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


def test_ensure_table_dims_grows_each_axis_in_one_call():
    table = _Table(1, 1)
    assert _ensure_table_dims(table, 40, 8) == (40, 8)
    assert table._rows.calls == [(1, 39)]
    assert table._cols.calls == [(1, 7)]


def test_ensure_table_dims_raises_when_growth_is_ignored():
    table = _Table(1, 1)
    table._rows.insertByIndex = lambda idx, n: None
    table._cols.insertByIndex = lambda idx, n: None
    with pytest.raises(ValueError, match="grew to 1x1"):
        _ensure_table_dims(table, 2, 2)


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
    with patch("plugin.writer.specialized.tables.is_draw", return_value=True):
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
        bridge.resolve_page.return_value = (page, 0)
        with patch("plugin.draw.tables._table_model", return_value=_Table(1, 1)):
            with patch("com.sun.star.awt.Point", MagicMock(), create=True), patch(
                "com.sun.star.awt.Size", MagicMock(), create=True
            ):
                out = insert_draw_table(ctx, rows=2, columns=2, data=[["h1", "h2"], ["v1", "v2"]])
    assert out["status"] == "ok"
    assert out["cells_written"] == 4
    page.add.assert_called_once_with(shape)


def test_insert_table_passes_active_page_index():
    ctx = MagicMock()
    ctx.active_page_index = 3
    shape = MagicMock()
    page = MagicMock()
    ctx.doc.createInstance.return_value = shape
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.resolve_page.return_value = (page, 3)
        with patch("plugin.draw.tables._table_model", return_value=_Table(1, 1)):
            with patch("com.sun.star.awt.Point", MagicMock(), create=True), patch(
                "com.sun.star.awt.Size", MagicMock(), create=True
            ):
                insert_draw_table(ctx, rows=2, columns=2)
    bridge.resolve_page.assert_called_once_with(None, active_page_index=3)


def test_insert_table_rejects_zero_rows():
    from plugin.framework.errors import ToolExecutionError

    with pytest.raises(ToolExecutionError) as exc_info:
        insert_draw_table(MagicMock(), rows=0, columns=2)
    assert "at least 1" in exc_info.value.message
    assert exc_info.value.code == "TOOL_EXECUTION_ERROR"


class DisposedException(Exception):
    """Type name matches is_disposal."""


@pytest.mark.parametrize(
    ("is_disposed", "page_idx"),
    [
        pytest.param(True, 0, id="disposed"),
        pytest.param(False, 4, id="bad-page"),
    ],
)
def test_insert_table_propagates_resolve_page_errors(is_disposed, page_idx):
    from plugin.framework.errors import ToolExecutionError

    ctx = MagicMock()
    ctx.active_page_index = 0
    exc = DisposedException("gone") if is_disposed else ToolExecutionError(f"Page index {page_idx} out of range.")
    expected_exc = DisposedException if is_disposed else ToolExecutionError
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.side_effect = exc
        with pytest.raises(expected_exc) as exc_info:
            insert_draw_table(ctx, rows=2, columns=2, page=page_idx)
    if not is_disposed:
        assert f"Page index {page_idx} out of range." in str(exc_info.value)


def test_table_insert_tool_dispatches_draw():
    ctx = MagicMock()
    with patch("plugin.writer.specialized.tables.is_draw", return_value=True):
        with patch("plugin.draw.tables.insert_draw_table", return_value={"status": "ok", "rows": 2}) as ins:
            out = TableInsert().execute(ctx, rows=2, columns=2, page=1)
    assert out["status"] == "ok"
    ins.assert_called_once_with(ctx, rows=2, columns=2, page=1)


def test_table_insert_draw_rejects_parent_cell():
    ctx = MagicMock()
    with patch("plugin.writer.specialized.tables.is_draw", return_value=True):
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
    with patch("plugin.writer.specialized.tables.is_draw", return_value=True):
        with patch(
            "plugin.draw.tables.delete_draw_table",
            return_value={"status": "ok", "table_name": "Fees"},
        ) as dele:
            out = TableDelete().execute(ctx, name=" Fees ", page=1, index=2)
    assert out["status"] == "ok"
    dele.assert_called_once_with(ctx.doc, name="Fees", page=1, index=2)


def test_get_draw_cells_empty_string_is_empty_cell():
    entry = {"model": _Table(1, 1)}
    assert get_draw_cells(entry) == [[""]]


def test_get_draw_cells_failed_read_is_not_empty_string():
    table = _Table(1, 1)
    cell = table.getCellByPosition(0, 0)
    cell.getString = MagicMock(side_effect=RuntimeError("read failed"))
    cell.getText = MagicMock(side_effect=RuntimeError("read failed"))
    entry = {"model": table}
    with pytest.raises(ValueError, match=r"row 0 col 0"):
        get_draw_cells(entry)


def test_get_draw_cells_disposed_read_propagates():
    table = _Table(1, 1)
    cell = table.getCellByPosition(0, 0)
    cell.getString = MagicMock(side_effect=DisposedException("gone"))
    entry = {"model": table}
    with pytest.raises(DisposedException):
        get_draw_cells(entry)


def test_table_get_cells_draw_failed_cell_is_error():
    table = _Table(1, 1)
    cell = table.getCellByPosition(0, 0)
    cell.getString = MagicMock(side_effect=RuntimeError("read failed"))
    cell.getText = MagicMock(side_effect=RuntimeError("read failed"))
    entry = {
        "model": table,
        "name": "MyTable",
        "page": 0,
        "index": 0,
        "rows": 1,
        "cols": 1,
    }
    ctx = MagicMock()
    with patch("plugin.writer.specialized.tables.is_draw", return_value=True):
        with patch("plugin.draw.tables.resolve_draw_table", return_value=entry):
            out = TableGetCells().execute(ctx, name="MyTable")
    assert out["status"] == "error"
    assert "row 0 col 0" in out["message"]
    assert "matrix" not in out


@pytest.fixture
def _table_insert_env():
    ctx = MagicMock()
    shape = MagicMock()
    page = MagicMock()
    page.getCount.return_value = 1
    ctx.doc.createInstance.return_value = shape
    with (
        patch("plugin.draw.bridge.DrawBridge") as bridge_cls,
        patch("com.sun.star.awt.Point", MagicMock(), create=True),
        patch("com.sun.star.awt.Size", MagicMock(), create=True),
    ):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        yield ctx, shape, page


@pytest.mark.parametrize(
    ("failure_stage", "exc", "match", "removes_shape", "data"),
    [
        pytest.param("size", RuntimeError("nope"), r"^Failed to size table", True, [["h1", "h2"], ["v1", "v2"]], id="size-failure-removes"),
        pytest.param("fill", RuntimeError("fill"), r"^Failed to fill table cells", True, [["h1", "h2"], ["v1", "v2"]], id="fill-failure-removes"),
        pytest.param("size", DisposedException("gone"), None, False, [["h1", "h2"], ["v1", "v2"]], id="size-disposed-propagates"),
        pytest.param("set_size", RuntimeError("size"), r"^Failed to create table", True, None, id="set-size-failure-removes"),
    ],
)
def test_insert_table_failure_cleanup(_table_insert_env, failure_stage, exc, match, removes_shape, data):
    from plugin.framework.errors import ToolExecutionError

    ctx, shape, page = _table_insert_env
    if failure_stage == "set_size":
        shape.setSize.side_effect = exc

    expected_exc = DisposedException if isinstance(exc, DisposedException) else ToolExecutionError
    with ExitStack() as stack:
        if failure_stage in ("size", "fill"):
            stack.enter_context(patch("plugin.draw.tables._table_model", return_value=_Table(1, 1)))
        if failure_stage == "size":
            stack.enter_context(patch("plugin.draw.tables._ensure_table_dims", side_effect=exc))
        elif failure_stage == "fill":
            stack.enter_context(patch("plugin.draw.tables._ensure_table_dims", return_value=(2, 2)))
            stack.enter_context(patch("plugin.draw.tables.fill_table_cells", side_effect=exc))
        with pytest.raises(expected_exc, match=match):
            insert_draw_table(ctx, rows=2, columns=2, data=data)

    if removes_shape:
        page.remove.assert_called_once_with(shape)
    else:
        page.remove.assert_not_called()


def test_table_model_reraises_disposed():
    from plugin.draw.tables import _table_model

    class DisposedException(Exception):
        pass

    shape = MagicMock(spec=["getModel"])
    shape.getModel.side_effect = DisposedException("closed")
    with pytest.raises(DisposedException):
        _table_model(shape)

    shape.getModel.side_effect = RuntimeError("not a model")
    assert _table_model(shape) is None


@pytest.mark.parametrize(
    ("rows", "columns", "data", "expected_message"),
    [
        pytest.param(1, 1, [["A", "B"]], "data row 0 has 2 columns but table only has 1.", id="too-many-cols"),
        pytest.param(1, 2, [["A", "B"], ["C", "D"]], "data has 2 rows but table only has 1.", id="too-many-rows"),
    ],
)
def test_insert_table_oversized_data_rejected_before_mutation(rows, columns, data, expected_message):
    from plugin.framework.errors import ToolExecutionError

    ctx = MagicMock()
    with pytest.raises(ToolExecutionError) as exc_info:
        insert_draw_table(ctx, rows=rows, columns=columns, data=data)
    assert exc_info.value.message == expected_message
    assert exc_info.value.code == "TOOL_EXECUTION_ERROR"
    ctx.doc.createInstance.assert_not_called()


