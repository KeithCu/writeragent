# doc_stubs.py
# Writer/Calc document stubs.
# Import via plugin.tests.testing_utils (this file is an implementation detail).

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock


def _register_doc_stubs_aliases() -> None:
    this = sys.modules[__name__]
    import plugin.tests  # noqa: F401
    import tests  # noqa: F401

    for name in ("plugin.tests.doc_stubs", "tests.doc_stubs"):
        sys.modules[name] = this


_register_doc_stubs_aliases()

# `com.sun.*` sys.modules keys snapshotted by the native runner.
_COM_SUN_STAR_MOCK_MODULE_KEYS = [
    "com",
    "com.sun",
    "com.sun.star",
    "com.sun.star.text",
    "com.sun.star.util",
    "com.sun.star.document",
    "com.sun.star.frame",
    "com.sun.star.beans",
    "com.sun.star.awt",
    "com.sun.star.task",
    "com.sun.star.lang",
    "com.sun.star.style",
    "com.sun.star.style.BreakType",
    "com.sun.star.ui",
    "com.sun.star.ui.UIElementType",
    "com.sun.star.container",
    "com.sun.star.uno",
    "com.sun.star.datatransfer",
    "com.sun.star.datatransfer.clipboard",
]

# UNO and core.* sys.modules keys that plugin.testing_runner.run_all_tests
# snapshots and restores between native suites. A suite that replaces one of
# these at import must not leave its stub for the next suite.
NATIVE_TEST_SYS_MODULE_SNAPSHOT_KEYS = (
    "uno",
    "unohelper",
    "unohelper.Base",
    *_COM_SUN_STAR_MOCK_MODULE_KEYS,
    "core",
    "core.logging",
    "core.async_stream",
    "core.config",
    "core.api",
    "core.document",
    "core.document_tools",
    "core.constants",
)


class ElementStub:
    def __init__(self, text, outline_level=0, services=None):
        self.text = text
        self.outline_level = outline_level
        self.services = services or ["com.sun.star.text.Paragraph"]

    def getString(self):
        return self.text

    def getPropertyValue(self, name):
        if name == "OutlineLevel":
            return self.outline_level
        from plugin.framework.errors import WriterAgentException
        raise WriterAgentException("Property not found")

    def supportsService(self, service):
        return service in self.services

    def getStart(self):
        return self # Stub for range

    def getEnd(self):
        return self

    def getText(self):
        return self

    def createEnumeration(self):
        from plugin.tests.testing_utils import FakeUnoEnum, FakeUnoPortion

        return FakeUnoEnum([FakeUnoPortion("Text", self.text)])

class WriterDocStub:
    def __init__(self, elements=None, doc_type="writer", items=None):
        self.elements = elements or []
        self.doc_type = doc_type
        self._items = items or {}
        self.url = f"test://{doc_type}"
        self._created = {}
        self._load_styles_calls = []

    def getText(self):
        class TextStub:
            def __init__(self, el):
                self.el = el

            def getStart(self):
                return self.el[0] if self.el else self

            def getEnd(self):
                return self.el[-1] if self.el else self

            def createTextCursor(self):
                return MockTextCursor()

            def createEnumeration(self):
                class EnumStub:
                    def __init__(self, el):
                        self.el = el
                        self.idx = 0

                    def hasMoreElements(self):
                        return self.idx < len(self.el)

                    def nextElement(self):
                        res = self.el[self.idx]
                        self.idx += 1
                        return res
                return EnumStub(self.el)
        return TextStub(self.elements)

    def supportsService(self, svc):
        if self.doc_type == "writer" and svc == "com.sun.star.text.TextDocument": return True
        if self.doc_type == "calc" and svc == "com.sun.star.sheet.SpreadsheetDocument": return True
        if self.doc_type == "draw" and svc == "com.sun.star.drawing.DrawingDocument": return True
        if self.doc_type == "impress" and svc == "com.sun.star.presentation.PresentationDocument": return True
        return False

    def getStyleFamilies(self):
        class FamiliesStub:
            def __init__(self, items, load_calls):
                self.items = items
                self._load_calls = load_calls
            def hasByName(self, name):
                return name in self.items
            def getByName(self, name):
                return self.items[name]
            def getElementNames(self):
                return tuple(self.items.keys())
            def loadStylesFromURL(self, url, props):
                self._load_calls.append((url, props))
        return FamiliesStub(self._items, self._load_styles_calls)

    def getMyItems(self):
        return self.getStyleFamilies()

    def createInstance(self, name):
        inst = self._created.get(name)
        if inst is None:
            inst = MagicMock(name=name)
            self._created[name] = inst
        return inst

    def lockControllers(self):
        pass

    def unlockControllers(self):
        pass

class MockDocument:
    def __init__(self):
        self.url = "test://mock"

    def supportsService(self, service):
        return False

class MockTextCursor:
    def __init__(self):
        pass

    def getStart(self): return self
    def getEnd(self): return self
    def getText(self): return self
    def getString(self): return ""
    def setString(self, val): pass
    def gotoStart(self, expand): pass
    def gotoEnd(self, expand): pass
    def goRight(self, count, expand): pass
    def goLeft(self, count, expand): pass
    def setPropertyValue(self, name, val): pass


# UNO CellContentType values (com.sun.star.table.CellContentType).
_CELL_EMPTY = 0
_CELL_VALUE = 1
_CELL_TEXT = 2
_CELL_FORMULA = 3


class _RangeAddress:
    __slots__ = ("StartColumn", "StartRow", "EndColumn", "EndRow", "Sheet")

    def __init__(self, start_col, start_row, end_col, end_row, sheet=0):
        self.StartColumn = start_col
        self.StartRow = start_row
        self.EndColumn = end_col
        self.EndRow = end_row
        self.Sheet = sheet


class _CellAddress:
    __slots__ = ("Column", "Row", "Sheet")

    def __init__(self, col, row, sheet=0):
        self.Column = col
        self.Row = row
        self.Sheet = sheet


def _stub_data_entry(cell: Any) -> Any:
    if cell.getType() == _CELL_VALUE:
        return cell.getValue()
    if cell.getType() == _CELL_FORMULA:
        return cell.getFormula()
    return cell.getString()


class CalcCellStub:
    """Stateful stand-in for a Calc cell / single-cell range."""

    def __init__(self, col=0, row=0, sheet=None):
        self._col = col
        self._row = row
        self._sheet = sheet
        self._string = ""
        self._value = 0.0
        self._formula = ""
        self._kind = _CELL_EMPTY  # empty | value | text | formula
        self._props: dict[str, Any] = {"NumberFormat": 0}

    def getString(self):
        return self._string

    def setString(self, value):
        self._string = "" if value is None else str(value)
        self._formula = ""
        self._value = 0.0
        self._kind = _CELL_TEXT if self._string else _CELL_EMPTY

    def getValue(self):
        return self._value

    def setValue(self, value):
        try:
            self._value = float(value)
        except (TypeError, ValueError):
            self._value = 0.0
        self._string = ""
        self._formula = ""
        self._kind = _CELL_VALUE

    def getFormula(self):
        return self._formula

    def setFormula(self, value):
        text = "" if value is None else str(value)
        self._formula = text
        if not text:
            self._string = ""
            self._value = 0.0
            self._kind = _CELL_EMPTY
        else:
            self._kind = _CELL_FORMULA

    def getType(self):
        return self._kind

    def getDataArray(self):
        return ((_stub_data_entry(self),),)

    def getFormulaArray(self):
        if self._kind == _CELL_FORMULA:
            entry = self._formula
        elif self._kind == _CELL_VALUE:
            entry = f"{self._value:.15G}"
        else:
            entry = self._string
        return ((entry,),)

    def clearContents(self, _flags=0):
        self._string = ""
        self._value = 0.0
        self._formula = ""
        self._kind = _CELL_EMPTY

    def getCellAddress(self):
        return _CellAddress(self._col, self._row)

    def getRangeAddress(self):
        return _RangeAddress(self._col, self._row, self._col, self._row)

    def getPropertyValue(self, name):
        return self._props.get(name)

    def setPropertyValue(self, name, val):
        self._props[name] = val

    def getSpreadsheet(self):
        return self._sheet


class CalcRangeStub:
    """Rectangular range backed by a CalcSheetStub grid."""

    def __init__(self, sheet, start_col, start_row, end_col, end_row):
        self._sheet = sheet
        self._start_col = start_col
        self._start_row = start_row
        self._end_col = end_col
        self._end_row = end_row

    def getRangeAddress(self):
        return _RangeAddress(self._start_col, self._start_row, self._end_col, self._end_row)

    def getCellByPosition(self, col, row):
        # Relative to range origin (UNO XCellRange).
        return self._sheet.getCellByPosition(self._start_col + col, self._start_row + row)

    def getDataArray(self):
        rows = []
        for r in range(self._start_row, self._end_row + 1):
            row_vals = [
                _stub_data_entry(self._sheet.getCellByPosition(c, r))
                for c in range(self._start_col, self._end_col + 1)
            ]
            rows.append(tuple(row_vals))
        return tuple(rows)

    def getFormulaArray(self):
        rows = []
        for r in range(self._start_row, self._end_row + 1):
            row_vals = []
            for c in range(self._start_col, self._end_col + 1):
                cell = self._sheet.getCellByPosition(c, r)
                # UNO getFormulaArray returns strings for everything, but specifically original text if not a formula.
                if cell.getType() == _CELL_FORMULA:
                    row_vals.append(cell.getFormula())
                elif cell.getType() == _CELL_VALUE:
                    row_vals.append(f"{cell.getValue():.15G}")
                else:
                    row_vals.append(cell.getString())
            rows.append(tuple(row_vals))
        return tuple(rows)

    def queryContentCells(self, flags):
        addrs = []
        if flags & 16:
            for r in range(self._start_row, self._end_row + 1):
                for c in range(self._start_col, self._end_col + 1):
                    if self._sheet.getCellByPosition(c, r).getType() == _CELL_FORMULA:
                        addrs.append(_RangeAddress(c, r, c, r))
        return SimpleNamespace(getRangeAddresses=lambda: tuple(addrs))

    def setDataArray(self, data):
        for r_off, row in enumerate(data or ()):
            for c_off, value in enumerate(row):
                cell = self._sheet.getCellByPosition(self._start_col + c_off, self._start_row + r_off)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cell.setValue(value)
                elif value is None or value == "":
                    cell.clearContents()
                else:
                    text = str(value)
                    if text.startswith("="):
                        cell.setFormula(text)
                    else:
                        cell.setString(text)

    def setFormulaArray(self, data):
        for r_off, row in enumerate(data or ()):
            for c_off, formula in enumerate(row):
                self._sheet.getCellByPosition(self._start_col + c_off, self._start_row + r_off).setFormula(str(formula))

    def setPropertyValue(self, name, val):
        for r in range(self._start_row, self._end_row + 1):
            for c in range(self._start_col, self._end_col + 1):
                self._sheet.getCellByPosition(c, r).setPropertyValue(name, val)

    def getFormulas(self):
        rows = []
        for r in range(self._start_row, self._end_row + 1):
            row_vals = []
            for c in range(self._start_col, self._end_col + 1):
                row_vals.append(self._sheet.getCellByPosition(c, r).getFormula())
            rows.append(tuple(row_vals))
        return tuple(rows)

    def getFormula(self):
        return self.getCellByPosition(0, 0).getFormula()

    def setFormula(self, value):
        self.getCellByPosition(0, 0).setFormula(value)

    def getString(self):
        return self.getCellByPosition(0, 0).getString()

    def setString(self, value):
        self.getCellByPosition(0, 0).setString(value)

    def getValue(self):
        return self.getCellByPosition(0, 0).getValue()

    def setValue(self, value):
        self.getCellByPosition(0, 0).setValue(value)

    def getType(self):
        return self.getCellByPosition(0, 0).getType()

    def clearContents(self, flags=0):
        for r in range(self._start_row, self._end_row + 1):
            for c in range(self._start_col, self._end_col + 1):
                self._sheet.getCellByPosition(c, r).clearContents(flags)

    def getSpreadsheet(self):
        return self._sheet


class CalcSheetStub:
    """Named sheet with an expandable cell grid."""

    def __init__(self, name="Sheet1", data=None):
        self._name = name
        self._cells = {}
        self._modify_listeners: list = []
        self.DrawPage = MagicMock(name=f"{name}.DrawPage")
        if data is not None:
            self._seed_data(data)

    def _seed_data(self, data):
        for row_idx, row in enumerate(data):
            for col_idx, value in enumerate(row):
                if value is None or value == "":
                    continue
                cell = self.getCellByPosition(col_idx, row_idx)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cell.setValue(value)
                else:
                    text = str(value)
                    if text.startswith("="):
                        cell.setFormula(text)
                    else:
                        cell.setString(text)

    def getName(self):
        return self._name

    def setName(self, name):
        self._name = str(name)

    def getColumns(self):
        return SimpleNamespace(getCount=lambda: 16384)

    def getRows(self):
        return SimpleNamespace(getCount=lambda: 1048576)

    def getCellByPosition(self, col, row):
        key = (int(col), int(row))
        cell = self._cells.get(key)
        if cell is None:
            cell = CalcCellStub(col=key[0], row=key[1], sheet=self)
            self._cells[key] = cell
        return cell

    def getCellRangeByPosition(self, start_col, start_row, end_col, end_row):
        return CalcRangeStub(self, int(start_col), int(start_row), int(end_col), int(end_row))

    def getCellRangeByName(self, name):
        from plugin.calc.address_utils import parse_range_string

        (start_col, start_row), (end_col, end_row) = parse_range_string(name)
        if start_col == end_col and start_row == end_row:
            return self.getCellByPosition(start_col, start_row)
        return self.getCellRangeByPosition(start_col, start_row, end_col, end_row)

    def addModifyListener(self, listener):
        self._modify_listeners.append(listener)

    def removeModifyListener(self, listener):
        try:
            self._modify_listeners.remove(listener)
        except ValueError:
            pass

    def createCursor(self):
        sheet = self

        class _UsedCursor:
            def gotoEndOfUsedArea(self, _expand=False):
                pass

            def getRangeAddress(self):
                non_empty = [(c, r) for (c, r), cell in sheet._cells.items() if cell.getType() != 0]
                if not non_empty:
                    return _RangeAddress(0, 0, 0, 0)
                return _RangeAddress(0, 0, max(c for c, _r in non_empty), max(r for _c, r in non_empty))

        return _UsedCursor()

    def queryContentCells(self, _flags=0):
        """Return formula cells as a UNO-like enum (CellFlags.FORMULA = 16 in production).

        Stub ignores *flags* and always enumerates formula cells — enough for pytest
        discovery paths that only query formulas.
        """
        formula_keys = [(c, r) for (c, r), cell in self._cells.items() if cell.getType() == _CELL_FORMULA]
        if not formula_keys:
            return _ContentCellsEnum([])
        cols = [c for c, _r in formula_keys]
        rows = [r for _c, r in formula_keys]
        rng = self.getCellRangeByPosition(min(cols), min(rows), max(cols), max(rows))
        return _ContentCellsEnum([rng])


class _ContentCellsEnum:
    """Minimal stand-in for XSheetCellRanges enumeration from queryContentCells."""

    def __init__(self, ranges):
        self._ranges = list(ranges)

    def getCount(self):
        return len(self._ranges)

    def getByIndex(self, index):
        return self._ranges[int(index)]


class CalcSheetsStub:
    """XSpreadsheets-like collection."""

    def __init__(self, sheets=None):
        self._sheets = {}
        self._order = []
        if sheets:
            for sheet in sheets:
                self._add(sheet)
        else:
            self._add(CalcSheetStub("Sheet1"))

    def _add(self, sheet):
        name = sheet.getName()
        if name not in self._sheets:
            self._order.append(name)
        self._sheets[name] = sheet

    def hasByName(self, name):
        return name in self._sheets

    def getByName(self, name):
        return self._sheets[name]

    def getByIndex(self, index):
        return self._sheets[self._order[int(index)]]

    def getCount(self):
        return len(self._order)

    def getElementNames(self):
        return tuple(self._order)

    def insertNewByName(self, name, index):
        sheet = CalcSheetStub(name)
        idx = max(0, min(int(index), len(self._order)))
        if name in self._sheets:
            self._sheets[name] = sheet
            return
        self._order.insert(idx, name)
        self._sheets[name] = sheet

    def removeByName(self, name):
        if name in self._sheets:
            del self._sheets[name]
        if name in self._order:
            self._order.remove(name)


class CalcControllerStub:
    """Current controller with both attribute and method access styles."""

    def __init__(self, active_sheet, selection=None):
        self.ActiveSheet = active_sheet
        self.Selection = selection if selection is not None else active_sheet.getCellByPosition(0, 0)

    def getActiveSheet(self):
        return self.ActiveSheet

    def getSelection(self):
        return self.Selection


class _UserDefinedPropertiesStub:
    def __init__(self):
        self.values = {}

    def hasByName(self, name):
        return name in self.values

    def getPropertyValue(self, name):
        if name not in self.values:
            raise KeyError(name)
        return self.values[name]

    def setPropertyValue(self, name, value):
        self.values[name] = value

    def addProperty(self, name, _attrs, value):
        self.values[name] = value


class CalcDocStub:
    """Stateful SpreadsheetDocument stub for pure pytest (no live LibreOffice).

    Defaults: one sheet ``Sheet1``, selection A1, ``url='test://calc'``.
    Seed a 2D grid with ``data=``; override selection / command values via kwargs.
    """

    def __init__(
        self,
        data=None,
        sheets=None,
        url="test://calc",
        command_values=None,
        selection=None,
        active_sheet=None,
        props=None,
        **_kwargs,
    ):
        if sheets is not None:
            sheet_list = list(sheets)
        else:
            sheet_list = [CalcSheetStub("Sheet1", data=data)]
        self._sheets = CalcSheetsStub(sheet_list)
        active = active_sheet
        if active is None:
            active = self._sheets.getByIndex(0)
        elif isinstance(active, str):
            active = self._sheets.getByName(active)
        if selection is None:
            selection = active.getCellByPosition(0, 0)
        elif isinstance(selection, str):
            selection = active.getCellRangeByName(selection)
        self._controller = CalcControllerStub(active, selection=selection)
        self.CurrentController = self._controller
        self.url = url
        self._command_values = command_values
        self._close_calls = []
        self._created = {}
        self._props = dict(props or {})
        self._user_defined_properties = _UserDefinedPropertiesStub()
        self._document_event_listeners = []
        self._calculate_all_calls = 0
        # Number-format supplier hooks for inspector/enrichment pytest (override via kwargs).
        self._number_formats = _kwargs.get("number_formats")
        if self._number_formats is None:
            self._number_formats = MagicMock(name="NumberFormats")
        self._null_date = _kwargs.get("null_date") or SimpleNamespace(Year=1899, Month=12, Day=30)

    def supportsService(self, svc):
        return svc == "com.sun.star.sheet.SpreadsheetDocument"

    def getDocumentProperties(self):
        class _DocProps:
            def __init__(self, user_props):
                self.UserDefinedProperties = user_props

        return _DocProps(self._user_defined_properties)

    def getSheets(self):
        return self._sheets

    def getCurrentController(self):
        return self._controller

    def getURL(self):
        return self.url

    def getNumberFormats(self):
        return self._number_formats

    def getNumberFormatSettings(self):
        settings = MagicMock(name="NumberFormatSettings")
        settings.getPropertyValue.return_value = self._null_date
        return settings

    def calculateAll(self):
        self._calculate_all_calls += 1

    @property
    def calculate_all_count(self):
        return self._calculate_all_calls

    def getCommandValues(self, _command=None):
        return self._command_values

    def getPropertyValue(self, name):
        if name not in self._props:
            raise KeyError(name)
        return self._props[name]

    def setPropertyValue(self, name, value):
        self._props[name] = value

    def setModified(self, modified):
        self._modified = bool(modified)

    def isModified(self):
        return getattr(self, "_modified", False)

    def addDocumentEventListener(self, listener):
        self._document_event_listeners.append(listener)

    def createInstance(self, name):
        inst = self._created.get(name)
        if inst is None:
            inst = MagicMock(name=name)
            self._created[name] = inst
        return inst

    def close(self, unused=True):
        self._close_calls.append(unused)

    def dispose(self):
        self.close(True)


class MockContext:
    """Mock context object used as a stand-in for the UNO ComponentContext outside of LibreOffice."""
    def __init__(self):
        self.mock_values = {}

    def getValueByName(self, name):
        return self.mock_values.get(name)

    def getServiceManager(self):
        return MagicMock()
