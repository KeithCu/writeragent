# testing_utils.py
# Centralized testing utilities and mocks for WriterAgent tests.

import contextlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

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
        sys.modules["uno"] = MagicMock()
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

    # Specific sub-module attachments (only when we fully mocked uno or installed fresh shells).
    class MockDate(object):
        Year = 2024
        Month = 1
        Day = 1

    setattr(sys.modules["com.sun.star.util"], "Date", MockDate)

    class MockListener(object):
        pass

    setattr(sys.modules["com.sun.star.awt"], "XActionListener", MockListener)

    class MockClipboardListener(object):
        pass

    setattr(
        sys.modules["com.sun.star.datatransfer.clipboard"],
        "XClipboardListener",
        MockClipboardListener,
    )

    class MockXCallback(object):
        pass

    setattr(sys.modules["com.sun.star.awt"], "XCallback", MockXCallback)

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

    setattr(sys.modules["com.sun.star.awt"], "XTextListener", MockXTextListener)

    class MockXWindowListener(object):
        pass

    setattr(sys.modules["com.sun.star.awt"], "XWindowListener", MockXWindowListener)

    class MockXKeyListener(object):
        pass

    setattr(sys.modules["com.sun.star.awt"], "XKeyListener", MockXKeyListener)

    class MockXEventListener(object):
        pass

    setattr(sys.modules["com.sun.star.lang"], "XEventListener", MockXEventListener)

    class MockXInitialization(object):
        pass

    setattr(sys.modules["com.sun.star.lang"], "XInitialization", MockXInitialization)

    class MockXServiceInfo(object):
        pass

    setattr(sys.modules["com.sun.star.lang"], "XServiceInfo", MockXServiceInfo)

    class MockXJobExecutor(object):
        pass

    setattr(sys.modules["com.sun.star.task"], "XJobExecutor", MockXJobExecutor)

    class MockXJob(object):
        pass

    setattr(sys.modules["com.sun.star.task"], "XJob", MockXJob)

    class MockXDispatch(object):
        pass

    setattr(sys.modules["com.sun.star.frame"], "XDispatch", MockXDispatch)

    class MockXDispatchProvider(object):
        pass

    setattr(sys.modules["com.sun.star.frame"], "XDispatchProvider", MockXDispatchProvider)
    setattr(sys.modules["com.sun.star.frame"], "DispatchDescriptor", MockBase)

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

    setattr(sys.modules["com.sun.star.container"], "NoSuchElementException", MockNoSuchElementException)
    setattr(sys.modules["com.sun.star.lang"], "DisposedException", MockDisposedException)
    setattr(sys.modules["com.sun.star.lang"], "IllegalArgumentException", MockIllegalArgumentException)
    setattr(sys.modules["com.sun.star.uno"], "RuntimeException", MockRuntimeException)
    setattr(sys.modules["com.sun.star.uno"], "Exception", MockUnoException)

    class MockXSidebarPanel:
        pass

    class MockXToolPanel:
        pass

    class MockXUIElement:
        pass

    class MockXUIElementFactory:
        pass

    setattr(sys.modules["com.sun.star.ui"], "XSidebarPanel", MockXSidebarPanel)
    setattr(sys.modules["com.sun.star.ui"], "XToolPanel", MockXToolPanel)
    setattr(sys.modules["com.sun.star.ui"], "XUIElement", MockXUIElement)
    setattr(sys.modules["com.sun.star.ui"], "XUIElementFactory", MockXUIElementFactory)

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

    def addModifyListener(self, _listener):
        pass

    def removeModifyListener(self, _listener):
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

# Experimental: wipe-and-reuse one hidden document per (ctx, type, hidden).
# Default ON for Calc only. Writer pooling still leaks CharWeight/HTML styles; pass reuse=True to try it.
_NATIVE_DOC_POOL: dict = {}

# GHA 33703959362: hang after insert_cell_html execute-done, before TEST end.
# When True, stderr breadcrumbs name reset_native_doc / _reset_calc_doc steps.
# with_native_doc sets this only for test_insert_cell_html (keep suite noise low).
_LOG_NATIVE_DOC_TEARDOWN = False


def _native_teardown_progress(msg: str) -> None:
    if not _LOG_NATIVE_DOC_TEARDOWN:
        return
    from plugin.testing_runner import _progress

    _progress(msg)


def _default_native_doc_reuse(doc_type: str) -> bool:
    return doc_type == "calc"

# offapi/com/sun/star/sheet/CellFlags.idl — VALUE|DATETIME|STRING|ANNOTATION|FORMULA|HARDATTR|STYLES|OBJECTS|EDITATTR|FORMATTED
_CALC_CLEAR_ALL = 1 | 2 | 4 | 8 | 16 | 32 | 64 | 128 | 256 | 512


def _native_doc_alive(doc) -> bool:
    try:
        doc.getCurrentController()
        return True
    except Exception:
        return False


def _clear_named_container(container) -> None:
    if container is None or not hasattr(container, "getElementNames"):
        return
    for name in list(container.getElementNames()):
        try:
            container.removeByName(name)
        except Exception:
            pass


def _clear_undo(doc) -> None:
    try:
        mgr = doc.getUndoManager()
        if mgr is not None:
            mgr.clear()
    except Exception:
        pass


def _remove_all_calc_charts(doc) -> None:
    sheets = doc.getSheets()
    for i in range(sheets.getCount()):
        try:
            charts = sheets.getByIndex(i).getCharts()
            for name in list(charts.getElementNames()):
                try:
                    charts.removeByName(name)
                except Exception:
                    pass
        except Exception:
            pass
    try:
        objs = doc.getEmbeddedObjects()
        for name in list(objs.getElementNames()):
            try:
                objs.removeByName(name)
            except Exception:
                pass
    except Exception:
        pass


def _clear_writeragent_udprops(doc) -> None:
    # GHA 33707990007: after insert_cell_html, Windows hung in
    # set_document_scripts → is_document_readonly_for_scripts → doc.isReadonly()
    # during calc native-doc wipe (remove_charts + clearContents had already
    # returned). A harness wipe is never a user-readonly save; write the
    # scripts UDProp the same way as the session ids so we never call
    # isReadonly(). Empty string is missing to get_document_scripts.
    try:
        from plugin.doc.udprops import set_document_property
        from plugin.scripting.document_scripts import DOCUMENT_SCRIPTS_UDPROP
        from plugin.scripting.session_manager import PYTHON_WORKBOOK_SESSION_PROP

        set_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, "")
        set_document_property(doc, PYTHON_WORKBOOK_SESSION_PROP, "")
        set_document_property(doc, "WriterAgentSessionID", "")
    except Exception:
        pass


def _reset_calc_doc(doc, ctx) -> None:  # ctx unused; same signature as writer reset
    _native_teardown_progress("native_doc: _reset_calc_doc start")
    _native_teardown_progress("native_doc: _reset_calc_doc remove_charts start")
    _remove_all_calc_charts(doc)
    _native_teardown_progress("native_doc: _reset_calc_doc remove_charts done")
    sheets = doc.getSheets()
    while sheets.getCount() > 1:
        name = sheets.getByIndex(sheets.getCount() - 1).Name
        sheets.removeByName(name)
    sheet = sheets.getByIndex(0)
    try:
        if sheet.Name != "Sheet1":
            sheet.setName("Sheet1")
    except Exception:
        pass
    try:
        _native_teardown_progress("native_doc: _reset_calc_doc clearContents start")
        cursor = sheet.createCursor()
        cursor.gotoStartOfUsedArea(False)
        cursor.gotoEndOfUsedArea(True)
        try:
            cursor.merge(False)
        except Exception:
            pass
        cursor.clearContents(_CALC_CLEAR_ALL)
        _native_teardown_progress("native_doc: _reset_calc_doc clearContents done")
    except Exception:
        _native_teardown_progress("native_doc: _reset_calc_doc clearContents fallback start")
        sheet.getCellRangeByName("A1:AMJ1048576").clearContents(_CALC_CLEAR_ALL)
        _native_teardown_progress("native_doc: _reset_calc_doc clearContents fallback done")
    _clear_named_container(getattr(doc, "NamedRanges", None))
    try:
        _clear_named_container(sheet.NamedRanges)
    except Exception:
        pass
    _clear_named_container(getattr(doc, "DatabaseRanges", None))
    try:
        import uno

        settings = doc.getNumberFormatSettings()
        nd = uno.createUnoStruct("com.sun.star.util.Date")
        nd.Year, nd.Month, nd.Day = 1899, 12, 30
        settings.setPropertyValue("NullDate", nd)
    except Exception:
        pass
    try:
        controller = doc.getCurrentController()
        controller.setActiveSheet(sheet)
        controller.select(sheet.getCellByPosition(0, 0))
    except Exception:
        pass
    _clear_writeragent_udprops(doc)
    _clear_undo(doc)
    _native_teardown_progress("native_doc: _reset_calc_doc done")


def _reset_writer_style_families(doc) -> None:
    """Drop user HTML styles and restore built-in CharWeight (Standard can pick up bold)."""
    try:
        families = doc.getStyleFamilies()
    except Exception:
        return
    for family_name in ("ParagraphStyles", "CharacterStyles"):
        try:
            styles = families.getByName(family_name)
        except Exception:
            continue
        for name in list(styles.getElementNames()):
            try:
                style = styles.getByName(name)
            except Exception:
                continue
            try:
                if bool(style.isUserDefined()):
                    styles.removeByName(name)
                    continue
            except Exception:
                pass
            for prop in ("CharWeight", "CharHeight", "CharPosture", "CharUnderline", "CharColor"):
                try:
                    style.setPropertyToDefault(prop)
                except Exception:
                    pass


def _writer_pool_is_clean(doc) -> bool:
    """False if wipe left text, bold, or graphics — caller should factory-load."""
    try:
        if (doc.getText().getString() or "").strip():
            return False
        cursor = doc.getText().createTextCursor()
        cursor.gotoStart(False)
        if float(cursor.getPropertyValue("CharWeight") or 100) >= 135.0:
            return False
        if hasattr(doc, "getGraphicObjects") and doc.getGraphicObjects().getCount() > 0:
            return False
    except Exception:
        return False
    return True


def _reset_writer_doc(doc, ctx) -> None:
    try:
        doc.setPropertyValue("RecordChanges", False)
    except Exception:
        pass
    try:
        smgr = ctx.getServiceManager()
        helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        frame = doc.getCurrentController().getFrame()
        helper.executeDispatch(frame, ".uno:AcceptAllTrackedChanges", "", 0, ())
    except Exception:
        pass
    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        cursor.setString("")
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        # Empty para keeps last run CharWeight/Heading; HTML insert at "end" with
        # apply_styles=False then paints body text with leftover bold (150).
        try:
            cursor.setPropertyValue("ParaStyleName", "Standard")
        except Exception:
            pass
        try:
            cursor.setPropertyValue("CharWeight", 100.0)
        except Exception:
            pass
        for prop in (
            "CharStyleName",
            "CharWeight",
            "CharHeight",
            "CharPosture",
            "CharUnderline",
            "CharColor",
            "CharBackColor",
            "CharEscapement",
            "CharFontName",
            "ParaAdjust",
        ):
            try:
                cursor.setPropertyToDefault(prop)
            except Exception:
                pass
        cursor.gotoStart(False)
        try:
            doc.getCurrentController().select(cursor)
        except Exception:
            pass
    except Exception:
        pass
    try:
        enum = doc.getText().createEnumeration()
        while enum.hasMoreElements():
            para = enum.nextElement()
            try:
                para.setPropertyValue("ParaStyleName", "Standard")
            except Exception:
                pass
            try:
                para.setPropertyValue("CharWeight", 100.0)
            except Exception:
                pass
            for prop in ("CharStyleName", "CharWeight", "CharHeight", "CharPosture"):
                try:
                    para.setPropertyToDefault(prop)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        smgr = ctx.getServiceManager()
        helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        frame = doc.getCurrentController().getFrame()
        helper.executeDispatch(frame, ".uno:SelectAll", "", 0, ())
        helper.executeDispatch(frame, ".uno:ResetAttributes", "", 0, ())
        cursor = doc.getText().createTextCursor()
        cursor.gotoStart(False)
        doc.getCurrentController().select(cursor)
    except Exception:
        pass
    for getter in ("getTextTables", "getTextFrames", "getGraphicObjects", "getEmbeddedObjects", "getTextSections"):
        if not hasattr(doc, getter):
            continue
        try:
            container = getattr(doc, getter)()
            for name in list(container.getElementNames()):
                try:
                    content = container.getByName(name)
                    doc.getText().removeTextContent(content)
                except Exception:
                    try:
                        container.getByName(name).dispose()
                    except Exception:
                        pass
        except Exception:
            pass
    _reset_writer_style_families(doc)
    _clear_undo(doc)


def reset_native_doc(doc, doc_type: str, ctx) -> None:
    """Wipe a Writer or Calc document so the next native test can reuse it."""
    if doc_type == "calc":
        _reset_calc_doc(doc, ctx)
    elif doc_type == "writer":
        _reset_writer_doc(doc, ctx)
    else:
        raise ValueError("reset_native_doc only supports writer and calc")


class TestingFactory:
    """Unified factory for creating test documents and contexts."""

    @staticmethod
    def create_doc(env="mock", doc_type="writer", content=None, **kwargs):
        """Create a mock document stub (or raise for native — use create_native_doc).

        - ``calc`` → :class:`CalcDocStub` (prefer ``data=`` 2D grid)
        - otherwise → :class:`WriterDocStub` (``content=`` paragraph list, ``items=`` style families)
        """
        if env == "native":
            raise NotImplementedError("Native doc creation requires a ctx. Use create_native_doc(ctx, ...)")

        if doc_type == "calc":
            calc_kwargs = dict(kwargs)
            if "data" not in calc_kwargs and content is not None and not isinstance(content, list):
                calc_kwargs["data"] = content
            return CalcDocStub(**calc_kwargs)

        elements = content if isinstance(content, list) else []
        return WriterDocStub(elements, doc_type=doc_type, **kwargs)

    @staticmethod
    def create_native_doc(ctx, doc_type="writer", hidden=True):
        """Creates a real hidden document in LibreOffice."""
        from plugin.framework.uno_context import get_desktop
        import uno

        desktop = get_desktop(ctx)
        props = []
        if hidden:
            props.append(uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True))
        
        if doc_type.startswith("private:") or doc_type.startswith("file://"):
            factory_url = doc_type
        else:
            factory_url = {
                "writer": "private:factory/swriter",
                "calc": "private:factory/scalc",
                "draw": "private:factory/sdraw",
                "impress": "private:factory/simpress"
            }.get(doc_type, "private:factory/swriter")

        doc = desktop.loadComponentFromURL(factory_url, "_blank", 0, tuple(props))
        return doc

    @staticmethod
    def close_doc(doc):
        """Safely closes a document instance if available."""
        if not doc:
            return
        for key, pooled in list(_NATIVE_DOC_POOL.items()):
            if pooled is doc:
                del _NATIVE_DOC_POOL[key]
        try:
            from plugin.scripting.session_manager import clear_active_calc_session

            clear_active_calc_session()
        except Exception:
            pass
        try:
            import gc

            # Release PyUNO sequences before Calc tears down the document.
            # Large getDataArray results held across close can abort soffice (glibc double-free).
            gc.collect()
            if hasattr(doc, "close"):
                doc.close(True)
            elif hasattr(doc, "dispose"):
                doc.dispose()
        except Exception:
            pass


    @staticmethod
    @contextlib.contextmanager
    def native_doc(ctx, doc_type="writer", hidden=True, reuse=None):
        """Yield a native LO document. Calc defaults to experimental wipe-and-reuse; Writer does not."""
        if reuse is None:
            reuse = _default_native_doc_reuse(doc_type)
        use_pool = bool(reuse) and doc_type in ("writer", "calc")
        doc = None
        pooled = False
        if use_pool:
            key = (id(ctx), doc_type, bool(hidden))
            candidate = _NATIVE_DOC_POOL.get(key)
            if candidate is not None and _native_doc_alive(candidate):
                try:
                    reset_native_doc(candidate, doc_type, ctx)
                    if doc_type == "writer" and not _writer_pool_is_clean(candidate):
                        TestingFactory.close_doc(candidate)
                        doc = None
                    else:
                        doc = candidate
                        pooled = True
                except Exception:
                    TestingFactory.close_doc(candidate)
                    doc = None
            if doc is None:
                doc = TestingFactory.create_native_doc(ctx, doc_type=doc_type, hidden=hidden)
                _NATIVE_DOC_POOL[key] = doc
                pooled = True
        else:
            doc = TestingFactory.create_native_doc(ctx, doc_type=doc_type, hidden=hidden)
        if doc_type == "calc" and doc is not None:
            try:
                from plugin.scripting.session_manager import calc_workbook_base_session_id

                calc_workbook_base_session_id(doc)
            except Exception:
                pass
        try:
            yield doc
        finally:
            if pooled:
                # Wipe before leaving the pool: tests without @with_native_doc still
                # see this document as the desktop's current component (init scripts, charts).
                _native_teardown_progress(
                    "native_doc: teardown reset start doc_type=%s" % doc_type
                )
                try:
                    reset_native_doc(doc, doc_type, ctx)
                except Exception:
                    _native_teardown_progress(
                        "native_doc: teardown reset failed; close_doc"
                    )
                    TestingFactory.close_doc(doc)
                    return
                _native_teardown_progress("native_doc: teardown reset done")
                try:
                    from plugin.scripting.session_manager import clear_active_calc_session

                    clear_active_calc_session()
                except Exception:
                    pass
            else:
                _native_teardown_progress("native_doc: teardown close_doc start")
                TestingFactory.close_doc(doc)
                _native_teardown_progress("native_doc: teardown close_doc done")



    @staticmethod
    def create_context(doc=None, ctx=None, env="mock", doc_type="writer", services=None, **ctx_kwargs):
        """Create a ToolContext for mock or native tests.

        Mock: builds a stub doc via :meth:`create_doc` when ``doc`` is omitted.
        Native: requires an existing ``doc`` (compose with ``@with_native_doc`` /
        :meth:`native_doc`); does not open documents itself.

        Pass ``services=`` to use the live plugin registry (``get_services()``) instead
        of a fresh ``ServiceRegistry``. Extra ``ctx_kwargs`` go to ``ToolContext``
        (e.g. ``status_callback``, ``active_page_index``).
        """
        from plugin.framework.tool import ToolContext
        from plugin.framework.service import ServiceRegistry

        if env == "mock":
            if doc is None:
                doc = TestingFactory.create_doc(env="mock", doc_type=doc_type)
            if ctx is None:
                ctx = MockContext()
            if services is None:
                services = ServiceRegistry()
            return ToolContext(doc=doc, ctx=ctx, doc_type=doc_type, services=services, caller="test", **ctx_kwargs)

        # Native env — caller owns document lifecycle (@with_native_doc).
        if doc is None:
            raise ValueError("create_context(env='native') requires doc= (use @with_native_doc)")
        if services is None:
            from plugin.doc.document_helpers import DocumentService
            from plugin.framework.event_bus import EventBus
            services = ServiceRegistry()
            services.register("document", DocumentService())
            services.register("events", EventBus())

        return ToolContext(doc=doc, ctx=ctx, doc_type=doc_type, services=services, caller="test", **ctx_kwargs)

    @staticmethod
    def execute_tool(doc, ctx, name, args=None, *, doc_type="calc", services=None, **ctx_kwargs):
        """Run a registered tool against a live (or stub) document.

        Defaults to ``get_services()`` so native Calc/Draw suites share one path.
        ``KeyError`` / ``ValueError`` from the registry become
        ``{"status": "error", "error": ...}`` (same contract as the old per-file helpers).
        """
        from plugin.main import get_tools, get_services

        if services is None:
            services = get_services()
        tctx = TestingFactory.create_context(
            doc=doc,
            ctx=ctx,
            env="native",
            doc_type=doc_type,
            services=services,
            **ctx_kwargs,
        )
        try:
            return get_tools().execute(name, tctx, **(args or {}))
        except (KeyError, ValueError) as e:
            return {"status": "error", "error": str(e)}


def with_native_doc(doc_type="writer", hidden=True, reuse=None):
    """Decorator to inject a native LibreOffice document into a test function and guarantee teardown.

    Calc: experimental wipe-and-reuse of one hidden spreadsheet (faster than factory+close).
    Writer: factory load/close by default (reuse leaks HTML/CharWeight). Pass reuse=True to try pooling.
    Draw/Impress never reuse.
    """
    def decorator(func):
        import functools
        import inspect

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Resolve ctx from args if present (native_test functions may receive ctx as first arg or kwargs)
            ctx = kwargs.get("ctx", None)
            if ctx is None and len(args) > 0:
                ctx = args[0]

            # GHA 33703959362: no TEST end after execute-done. Enter/exit here
            # splits a body hang from wipe-and-reuse teardown after return.
            log_teardown = func.__name__ == "test_insert_cell_html"
            global _LOG_NATIVE_DOC_TEARDOWN
            prev_teardown_log = _LOG_NATIVE_DOC_TEARDOWN
            if log_teardown:
                _LOG_NATIVE_DOC_TEARDOWN = True
                from plugin.testing_runner import _progress

                _progress(
                    "with_native_doc: enter name=%s doc_type=%s" % (func.__name__, doc_type)
                )
            try:
                with TestingFactory.native_doc(ctx, doc_type=doc_type, hidden=hidden, reuse=reuse) as doc:
                    sig = inspect.signature(func)
                    call_kwargs = {}
                    # Inject by parameter name so ctx is never dropped when doc is added.
                    if "ctx" in sig.parameters:
                        call_kwargs["ctx"] = ctx
                    if "doc" in sig.parameters:
                        call_kwargs["doc"] = doc
                    if call_kwargs:
                        result = func(**call_kwargs)
                    elif len(sig.parameters) == 1:
                        result = func(doc)
                    else:
                        result = func(*args, **kwargs)
                    if log_teardown:
                        from plugin.testing_runner import _progress

                        _progress(
                            "with_native_doc: body returned name=%s; teardown start"
                            % func.__name__
                        )
                    return result
            finally:
                _LOG_NATIVE_DOC_TEARDOWN = prev_teardown_log
                if log_teardown:
                    from plugin.testing_runner import _progress

                    _progress("with_native_doc: teardown done name=%s" % func.__name__)
        return wrapper
    return decorator

def create_mock_client():
    """Creates a pre-configured MagicMock for an LlmClient."""
    from unittest.mock import MagicMock
    mock_client = MagicMock()
    mock_client.config = MagicMock()
    mock_client.config.get.return_value = False
    return mock_client

def create_mock_http_response(
    status_code=200,
    json_data=None,
    *,
    reason=None,
    body=None,
    sse_lines=None,
    iter_side_effect=None,
    headers=None,
):
    """Mock ``http.client.HTTPResponse`` for pytest (no UNO, no live HTTP).

    * ``json_data`` / ``body`` feed sync ``response.read()``.
    * ``sse_lines`` feeds ``for line in response`` / ``iterate_sse`` (bytes or str).
    * ``iter_side_effect`` is raised after those lines (timeout / connection reset
      mid-stream). HTTP 4xx/5xx use ``status`` + ``reason`` + body. ``LlmClient``
      retries 429/503 up to three total attempts with backoff; other statuses raise immediately.
    """
    from unittest.mock import MagicMock
    import http.client
    import json

    mock_resp = MagicMock()
    mock_resp.status = status_code
    mock_resp.reason = (
        reason if reason is not None else http.client.responses.get(status_code, "")
    )
    header_map = dict(headers or {})

    def _getheader(name, default=None):
        return header_map.get(name, header_map.get(str(name).lower(), default))

    mock_resp.getheader.side_effect = _getheader

    if body is None and json_data is not None:
        body = json.dumps(json_data).encode("utf-8")
    mock_resp.read.return_value = b"" if body is None else body

    lines = []
    if sse_lines is not None:
        for line in sse_lines:
            if isinstance(line, str):
                line = line.encode("utf-8")
            if not line.endswith(b"\n"):
                line = line + b"\n"
            lines.append(line)

    if iter_side_effect is not None:
        def _iter():
            yield from lines
            raise iter_side_effect

        # return_value (not side_effect): ``for line in response`` matches
        # existing LlmClient tests that set ``__iter__.return_value = iter(...)``.
        mock_resp.__iter__.return_value = _iter()
    else:
        mock_resp.__iter__.return_value = iter(lines)
    return mock_resp