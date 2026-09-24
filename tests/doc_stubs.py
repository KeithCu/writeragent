# doc_stubs.py
# Pytest UNO shells and Writer/Calc document stubs.
# Import via plugin.tests.testing_utils (this file is an implementation detail).

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock


def _register_doc_stubs_aliases() -> None:
    this = sys.modules[__name__]
    import plugin.tests  # noqa: F401
    import tests  # noqa: F401

    for name in ("plugin.tests.doc_stubs", "tests.doc_stubs"):
        sys.modules[name] = this


_register_doc_stubs_aliases()

# `com.sun.*` names created/updated by setup_uno_mocks (for-loop).
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

# Every sys.modules key setup_uno_mocks assigns, plus core.* used by some uno tests.
# plugin.testing_runner.run_all_tests snapshots/restores this list between native suites.
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


def setup_uno_mocks():
    """
    Centralized function to mock LibreOffice UNO dependencies for testing outside of LibreOffice.
    This must be called at the top of test files before importing the module under test.
    """
    # Real `uno` is a types.ModuleType (embedded LibreOffice PyUNO or types-unopy in the venv).
    # Never replace it with MagicMock — that breaks in-LO native tests (e.g. uno.createUnoStruct).
    # Still create missing `com.sun.star.*` shell modules for pytest without a full bridge: types-unopy
    # provides `uno` but often has not loaded `com.sun.star.lang` etc. yet.
    try:
        import uno  # noqa: F401
    except ImportError:
        uno_import_ok = False
    else:
        uno_import_ok = True

    um = sys.modules.get("uno")
    use_magicmock_uno = not (uno_import_ok and isinstance(um, types.ModuleType))

    class MockBase(object):
        pass

    if use_magicmock_uno:
        # Pytest conftest already installed these. Replacing ``uno`` discards the
        # session mock, and replacing conftest's unohelper ModuleType with MagicMock
        # drops ImplementationHelper. Only fill gaps (direct ``python file.py`` runs).
        if not isinstance(sys.modules.get("uno"), MagicMock):
            sys.modules["uno"] = MagicMock()
        uh = sys.modules.get("unohelper")
        if uh is None or (isinstance(uh, MagicMock) and not hasattr(uh, "Base")):
            sys.modules["unohelper"] = MagicMock()
            # We must use types.ModuleType and attach empty classes to avoid 'metaclass conflict' with ty
            sys.modules["unohelper"].Base = MockBase
            sys.modules["unohelper.Base"] = MockBase

    created_com_shells: set[str] = set()
    for mod in _COM_SUN_STAR_MOCK_MODULE_KEYS:
        cur = sys.modules.get(mod)
        if cur is None or isinstance(cur, MagicMock):
            sys.modules[mod] = types.ModuleType(mod)
            created_com_shells.add(mod)

    # Do not setattr test doubles onto real bridge-loaded com.sun.star.* modules (embedded LO).
    if not use_magicmock_uno and not created_com_shells:
        return

    def _ensure(module_name: str, attr: str, value: object) -> None:
        # Skip attributes conftest (or an earlier call) already installed. Overwriting
        # exception and listener classes splits identity across modules imported earlier.
        mod = sys.modules.get(module_name)
        if mod is None or hasattr(mod, attr):
            return
        setattr(mod, attr, value)

    # Specific sub-module attachments (only when we fully mocked uno or installed fresh shells).
    class MockDate(object):
        Year = 2024
        Month = 1
        Day = 1

    _ensure("com.sun.star.util", "Date", MockDate)

    class MockListener(object):
        pass

    _ensure("com.sun.star.awt", "XActionListener", MockListener)

    class MockClipboardListener(object):
        pass

    _ensure("com.sun.star.datatransfer.clipboard", "XClipboardListener", MockClipboardListener)

    class MockXCallback(object):
        pass

    _ensure("com.sun.star.awt", "XCallback", MockXCallback)

    awt_mod = sys.modules.get("com.sun.star.awt")
    if awt_mod is not None and not hasattr(awt_mod, "Size"):

        class MockSize:
            def __init__(self, width=0, height=0):
                self.Width = width
                self.Height = height

        class MockPoint:
            def __init__(self, x=0, y=0):
                self.X = x
                self.Y = y

        setattr(awt_mod, "Size", MockSize)
        setattr(awt_mod, "Point", MockPoint)

    class MockXTextListener(object):
        pass

    _ensure("com.sun.star.awt", "XTextListener", MockXTextListener)

    class MockXWindowListener(object):
        pass

    _ensure("com.sun.star.awt", "XWindowListener", MockXWindowListener)

    class MockXKeyListener(object):
        pass

    _ensure("com.sun.star.awt", "XKeyListener", MockXKeyListener)

    class MockXEventListener(object):
        pass

    _ensure("com.sun.star.lang", "XEventListener", MockXEventListener)

    class MockXInitialization(object):
        pass

    _ensure("com.sun.star.lang", "XInitialization", MockXInitialization)

    class MockXServiceInfo(object):
        pass

    _ensure("com.sun.star.lang", "XServiceInfo", MockXServiceInfo)

    class MockXJobExecutor(object):
        pass

    _ensure("com.sun.star.task", "XJobExecutor", MockXJobExecutor)

    class MockXJob(object):
        pass

    _ensure("com.sun.star.task", "XJob", MockXJob)

    class MockXDispatch(object):
        pass

    _ensure("com.sun.star.frame", "XDispatch", MockXDispatch)

    class MockXDispatchProvider(object):
        pass

    _ensure("com.sun.star.frame", "XDispatchProvider", MockXDispatchProvider)
    _ensure("com.sun.star.frame", "DispatchDescriptor", MockBase)

    # Fresh shells replace conftest MagicMock beans; image_tools imports PropertyValue at load time.
    beans_mod = sys.modules.get("com.sun.star.beans")
    if beans_mod is not None and not hasattr(beans_mod, "PropertyValue"):

        class MockPropertyValue:
            def __init__(self, Name=None, Value=None):
                self.Name = Name
                self.Value = Value

        setattr(beans_mod, "PropertyValue", MockPropertyValue)

    class MockNoSuchElementException(Exception):
        pass

    class MockDisposedException(Exception):
        pass

    class MockIllegalArgumentException(Exception):
        pass

    class MockRuntimeException(Exception):
        pass

    class MockUnoException(Exception):
        pass

    _ensure("com.sun.star.container", "NoSuchElementException", MockNoSuchElementException)
    _ensure("com.sun.star.lang", "DisposedException", MockDisposedException)
    _ensure("com.sun.star.lang", "IllegalArgumentException", MockIllegalArgumentException)
    _ensure("com.sun.star.uno", "RuntimeException", MockRuntimeException)
    _ensure("com.sun.star.uno", "Exception", MockUnoException)

    class MockXSidebarPanel:
        pass

    class MockXToolPanel:
        pass

    class MockXUIElement:
        pass

    class MockXUIElementFactory:
        pass

    _ensure("com.sun.star.ui", "XSidebarPanel", MockXSidebarPanel)
    _ensure("com.sun.star.ui", "XToolPanel", MockXToolPanel)
    _ensure("com.sun.star.ui", "XUIElement", MockXUIElement)
    _ensure("com.sun.star.ui", "XUIElementFactory", MockXUIElementFactory)

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
            def __init__(self, items):
                self.items = items
            def hasByName(self, name):
                return name in self.items
            def getByName(self, name):
                return self.items[name]
            def getElementNames(self):
                return tuple(self.items.keys())
        return FamiliesStub(self._items)

    def getMyItems(self):
        return self.getStyleFamilies()

    def createInstance(self, name):
        inst = self._created.get(name)
        if inst is None:
            inst = MagicMock(name=name)
            self._created[name] = inst
        return inst

    def loadStylesFromURL(self, url, props):
        self._load_styles_calls.append((url, props))

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

    def clearContents(self, _flags=0):
        self._string = ""
        self._value = 0.0
        self._formula = ""
        self._kind = _CELL_EMPTY

    def getCellAddress(self):
        return _CellAddress(self._col, self._row)

    def getRangeAddress(self):
        return _RangeAddress(self._col, self._row, self._col, self._row)

    def getPropertyValue(self, _name):
        return None

    def setPropertyValue(self, _name, _val):
        pass

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
            row_vals = []
            for c in range(self._start_col, self._end_col + 1):
                cell = self._sheet.getCellByPosition(c, r)
                if cell.getType() == _CELL_VALUE:
                    row_vals.append(cell.getValue())
                elif cell.getType() == _CELL_FORMULA:
                    row_vals.append(cell.getFormula())
                else:
                    row_vals.append(cell.getString())
            rows.append(tuple(row_vals))
        return tuple(rows)

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


class CalcControllerStub:
    """Current controller with both attribute and method access styles."""

    def __init__(self, active_sheet, selection=None):
        self.ActiveSheet = active_sheet
        self.Selection = selection if selection is not None else active_sheet.getCellByPosition(0, 0)

    def getActiveSheet(self):
        return self.ActiveSheet

    def getSelection(self):
        return self.Selection


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
        self._document_event_listeners = []
        self._calculate_all_calls = 0
        # Number-format supplier hooks for inspector/enrichment pytest (override via kwargs).
        self._number_formats = _kwargs.get("number_formats")
        if self._number_formats is None:
            self._number_formats = MagicMock(name="NumberFormats")
        self._null_date = _kwargs.get("null_date") or SimpleNamespace(Year=1899, Month=12, Day=30)

    def supportsService(self, svc):
        return svc == "com.sun.star.sheet.SpreadsheetDocument"

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
